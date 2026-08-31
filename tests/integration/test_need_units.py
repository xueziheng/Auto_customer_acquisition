"""真实隔离Postgres单位事实；权限/客户来源受控，不宣称Gateway验收。"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from domains.demand import schemas
from domains.demand.errors import (
    NeedUnitError,
    NeedUnitPermissionError,
    NeedUnitUnavailableError,
)
from domains.demand.service import (
    DemandService,
    NeedUnitService,
    quantity_fact_hash,
    require_current_unit,
)
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.need_unit_uow import SqlAlchemyNeedUnitUnitOfWork
from infra.db.tables import (
    ConversationRow,
    MessageRow,
    RawArtifactRow,
    ValidatedNeedFieldHistoryRow,
    ValidatedNeedRow,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import EmployeeId, TenantId, ValidatedNeedId, new_id
from shared.schemas.provenance import FactualField, Provenance, SourceType

NOW = datetime(2026, 8, 28, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def migrate(engine: AsyncEngine, *args: str) -> int:
    """仅连接调用者创建的隔离库，关闭dotenv且不输出连接或SQL异常。"""
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *args],
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": engine.url.render_as_string(False),
            "PYTHON_DOTENV_DISABLED": "1",
        },
        capture_output=True,
        check=False,
    )
    return result.returncode


@pytest_asyncio.fixture
async def unit_engine(integration_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """每例独立数据库，避免不可变证据污染旧迁移往返；绝不使用生产DSN。"""
    name = "test_need_unit_" + uuid4().hex
    async with integration_engine.connect() as connection:
        admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(integration_engine.url.set(database=name))
    try:
        assert migrate(engine, "upgrade", "head") == 0, "隔离库迁移失败"
        yield engine
    finally:
        await engine.dispose()
        async with integration_engine.connect() as connection:
            admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{name}"'))


class ControlledAccess:
    """只证明服务调用顺序，真实员工/归属锁由T8验收。"""

    def __init__(
        self, tenant: TenantId, need: ValidatedNeedId, actor: EmployeeId
    ) -> None:
        self.access = schemas.NeedUnitAccess(
            tenant_id=tenant,
            need_id=need,
            actor_id=actor,
            opportunity_id="opp_controlled",
            account_id="acct_controlled",
            authorization_ref="test",
        )
        self._depth: ContextVar[int] = ContextVar("need_unit_guard_depth", default=0)

    @property
    def depth(self) -> int:
        """持锁深度按协程隔离，不能把另一并发请求的锁计入当前来源读取。"""
        return self._depth.get()

    async def check(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        *,
        action: schemas.NeedUnitAction,
    ) -> schemas.NeedUnitAccess:
        """受控身份必须匹配租户/需求/员工。"""
        if (tenant_id, need_id, actor_id) != (
            self.access.tenant_id,
            self.access.need_id,
            self.access.actor_id,
        ):
            raise NeedUnitPermissionError("permission_denied")
        return self.access

    @asynccontextmanager
    async def guard(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        *,
        action: schemas.NeedUnitAction,
    ) -> AsyncIterator[schemas.NeedUnitAccess]:
        """按当前协程记录授权保护，真实锁由T8接续。"""
        result = await self.check(tenant_id, need_id, actor_id, action=action)
        token = self._depth.set(self.depth + 1)
        try:
            yield result
        finally:
            self._depth.reset(token)


class ControlledReader:
    """真实消息行/原件由fixture提供，原文关系核验是受控而非live。"""

    def __init__(self, access: ControlledAccess, artifact_id: str) -> None:
        self.access, self.artifact_id = access, artifact_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.calls = 0
        self.denied = False

    async def read_verified(
        self, query: schemas.NeedUnitEvidenceQuery
    ) -> schemas.VerifiedNeedUnitEvidence:
        assert self.access.depth == 0
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return schemas.VerifiedNeedUnitEvidence(
            tenant_id=query.tenant_id,
            need_id=query.need_id,
            account_id=query.account_id,
            source_message_id=query.source_message_id,
            artifact_id=self.artifact_id,
            content_hash="a" * 64,
            locator=query.locator,
            source_quote=query.source_quote,
            unit=query.unit,
            quantity_fact_hash=query.quantity_fact_hash,
            observed_at=NOW,
        )

    async def authorize_reference(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        source: schemas.VerifiedNeedUnitEvidence,
    ) -> None:
        """仅模拟当前资料阅读权，不重取原文。"""
        assert self.access.depth == 0
        if self.denied:
            raise NeedUnitPermissionError("permission_denied")


@dataclass
class UnitDbCase:
    tenant: TenantId
    need_id: ValidatedNeedId
    actor_id: EmployeeId
    service: NeedUnitService
    command: schemas.NeedUnitConfirmationCommand
    demand: DemandService
    factory: Callable[[TenantId], SqlAlchemyNeedUnitUnitOfWork]
    sessions: async_sessionmaker[AsyncSession]
    reader: ControlledReader
    engine: AsyncEngine

    async def confirmation_count(self) -> int:
        """新连接核对同租户同Need持久记录。"""
        async with self.sessions() as session:
            return (
                await session.execute(
                    text(
                        "SELECT count(*) FROM need_unit_confirmations "
                        "WHERE tenant_id=:tenant AND need_id=:need"
                    ),
                    {"tenant": self.tenant, "need": self.need_id},
                )
            ).scalar_one()

    async def unit_history_count(self) -> int:
        """新连接只计unit历史。"""
        async with self.sessions() as session:
            return (
                await session.execute(
                    select(func.count())
                    .select_from(ValidatedNeedFieldHistoryRow)
                    .where(
                        ValidatedNeedFieldHistoryRow.tenant_id == self.tenant,
                        ValidatedNeedFieldHistoryRow.need_id == self.need_id,
                        ValidatedNeedFieldHistoryRow.field_name == "unit",
                    )
                )
            ).scalar_one()

    async def confirm(
        self,
        *,
        key: str = "first",
        command: schemas.NeedUnitConfirmationCommand | None = None,
    ) -> schemas.NeedUnitConfirmationView:
        """通过真实服务而非仓储捷径执行。"""
        return await self.service.confirm(
            self.tenant,
            self.need_id,
            command or self.command,
            actor_id=self.actor_id,
            idempotency_key=key,
        )

    async def change_quantity(self, value: int = 600) -> None:
        """保留旧更新API，使用存在的受控客户消息。"""
        await self.demand.update_need_fields(
            self.tenant,
            self.need_id,
            {
                "quantity": {
                    "value": value,
                    "quote": f"We now need {value} pieces.",
                    "extracted_by": str(self.actor_id),
                }
            },
            source_message_id="msg_customer_changed",
            updated_by=str(self.actor_id),
        )


@pytest_asyncio.fixture
async def unit_db_case(unit_engine: AsyncEngine) -> UnitDbCase:
    """真实UoW/service/消息/原件夹具，依赖不存在应表现为明确契约RED。"""
    async with unit_engine.connect() as connection:
        names = await connection.run_sync(lambda c: inspect(c).get_table_names())
    assert "need_unit_confirmations" in names, "缺少0042单位确认存储"
    uow_type = importlib.import_module(
        "infra.db.need_unit_uow"
    ).SqlAlchemyNeedUnitUnitOfWork
    service_type = importlib.import_module(
        "domains.demand.unit_service"
    ).NeedUnitServiceImpl
    tenant, need, actor = (
        TenantId(new_id("tn")),
        ValidatedNeedId(new_id("nd")),
        EmployeeId("emp_test"),
    )
    artifact = new_id("art")
    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    quantity = FactualField(
        500,
        Provenance(
            SourceType.CONVERSATION,
            "msg_customer_1",
            str(actor),
            NOW,
            actor,
            NOW,
            source_quote="We need 500 pieces.",
        ),
    )
    old_repo = importlib.import_module("infra.db.repositories.need_hypotheses")
    async with sessions.begin() as session:
        session.add(
            ConversationRow(
                tenant_id=tenant,
                conversation_id="conv_test",
                account_id="acct_controlled",
                channel="email",
                created_at=NOW,
            )
        )
        session.add(
            RawArtifactRow(
                tenant_id=tenant,
                artifact_id=artifact,
                kind="email_raw",
                content_hash="a" * 64,
                size_bytes=19,
                mime_type="message/rfc822",
                object_key=f"raw/{tenant}/{artifact}",
                uploaded_at=NOW,
            )
        )
        await session.flush()
        for msg in ("msg_customer_1", "msg_customer_changed"):
            session.add(
                MessageRow(
                    tenant_id=tenant,
                    message_id=msg,
                    conversation_id="conv_test",
                    direction="inbound",
                    sent_at=NOW,
                    raw_artifact_ref=artifact,
                    external_message_id=msg,
                )
            )
        session.add(
            ValidatedNeedRow(
                tenant_id=tenant,
                need_id=need,
                account_id="acct_controlled",
                source_message_id="msg_customer_1",
                created_at=NOW,
                product_category=old_repo._factual_to_json(
                    FactualField("hinges", quantity.provenance)
                ),
                quantity=old_repo._factual_to_json(quantity),
            )
        )
    access = ControlledAccess(tenant, need, actor)
    reader = ControlledReader(access, artifact)
    factory = lambda tid: uow_type(
        sessions, tid, lock_timeout_ms=1500, statement_timeout_ms=3000
    )
    svc = service_type(factory, access, reader, now=lambda: NOW)
    demand_type = importlib.import_module(
        "domains.demand.service_impl"
    ).DemandServiceImpl
    demand = demand_type(
        lambda tid: SqlAlchemyDemandUnitOfWork(sessions, tid, now=lambda: NOW),
        now=lambda: NOW,
    )
    command = schemas.NeedUnitConfirmationCommand(
        unit="pieces",
        source_message_id="msg_customer_1",
        locator="body:0:19",
        source_quote="We need 500 pieces.",
        expected_quantity_fact_hash=quantity_fact_hash(tenant, need, quantity),
        expected_unit_confirmation_id=None,
    )
    return UnitDbCase(
        tenant,
        need,
        actor,
        svc,
        command,
        demand,
        factory,
        sessions,
        reader,
        unit_engine,
    )


@pytest.mark.parametrize("value", [500, 600])
async def test_old_quantity_update_invalidates_binding(
    unit_db_case: UnitDbCase, value: int
) -> None:
    c = unit_db_case
    first = await c.confirm()
    await c.change_quantity(value)
    facts = await c.service.get_facts(c.tenant, c.need_id, actor_id=c.actor_id)
    assert facts.unit_confirmation_id == first.confirmation_id
    with pytest.raises(NeedUnitError) as caught:
        require_current_unit(facts)
    assert caught.value.code == "unit_stale"
    assert await c.confirm() == first
    assert await c.confirmation_count() == await c.unit_history_count() == 1
    async with SqlAlchemyDemandUnitOfWork(c.sessions, c.tenant) as old:
        need = await old.needs.get(c.tenant, c.need_id)
        assert need.unit == first.unit


async def test_two_same_keys_commit_one_receipt(unit_db_case: UnitDbCase) -> None:
    c = unit_db_case
    first, second = await asyncio.gather(
        c.confirm(), c.confirm(), return_exceptions=True
    )
    assert not isinstance(first, BaseException) and not isinstance(
        second, BaseException
    )
    assert first == second
    assert await c.confirmation_count() == await c.unit_history_count() == 1
    assert (
        await c.service.get_facts(c.tenant, c.need_id, actor_id=c.actor_id)
    ).unit == first.unit


@pytest.mark.parametrize("same_key", [True, False])
async def test_competing_payload_or_expected_id_cannot_double_write(
    unit_db_case, same_key
) -> None:
    c = unit_db_case
    results = await asyncio.gather(
        c.confirm(),
        c.confirm(
            key="first" if same_key else "second",
            command=c.command.model_copy(update={"unit": "boxes"}),
        ),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, NeedUnitError)]
    assert len(errors) == 1
    assert errors[0].code == ("idempotency_conflict" if same_key else "unit_changed")
    assert await c.confirmation_count() == await c.unit_history_count() == 1


async def test_source_read_is_zero_need_locks_and_quantity_race_fails(
    unit_db_case,
) -> None:
    c = unit_db_case
    c.reader.release.clear()
    task = asyncio.create_task(c.confirm())
    try:
        await asyncio.wait_for(c.reader.entered.wait(), 3)
        async with c.sessions.begin() as session:
            await session.execute(
                select(ValidatedNeedRow)
                .where(
                    ValidatedNeedRow.tenant_id == c.tenant,
                    ValidatedNeedRow.need_id == c.need_id,
                )
                .with_for_update(nowait=True)
            )
        await c.change_quantity()
    finally:
        c.reader.release.set()
    with pytest.raises(NeedUnitError) as caught:
        await task
    assert caught.value.code == "quantity_changed"
    assert await c.confirmation_count() == await c.unit_history_count() == 0


async def test_database_rejects_mutation_and_receipt_mismatch(unit_db_case) -> None:
    c = unit_db_case
    first = await c.confirm()
    for sql in (
        "UPDATE need_unit_confirmations SET confirmed_by='other' WHERE tenant_id=:tenant AND need_id=:need",
        "DELETE FROM need_unit_confirmations WHERE tenant_id=:tenant AND need_id=:need",
        "UPDATE validated_needs SET unit_quantity_fact_hash=repeat('f',64) WHERE tenant_id=:tenant AND need_id=:need",
        "UPDATE validated_needs SET unit=NULL,unit_quantity_fact_hash=NULL,unit_confirmation_id=NULL WHERE tenant_id=:tenant AND need_id=:need",
    ):
        with pytest.raises(DBAPIError):
            async with c.sessions.begin() as session:
                await session.execute(
                    text(sql), {"tenant": c.tenant, "need": c.need_id}
                )
    assert await c.confirm() == first


async def test_history_failure_rolls_back_three_writes(
    unit_db_case, monkeypatch
) -> None:
    c = unit_db_case
    repo_type = importlib.import_module(
        "infra.db.repositories.need_units"
    ).NeedUnitRepositoryImpl

    async def fail(self, *args):
        raise NeedUnitError("invalid_input")

    monkeypatch.setattr(repo_type, "append_unit_history", fail)
    with pytest.raises(NeedUnitError):
        await c.confirm()
    assert await c.confirmation_count() == await c.unit_history_count() == 0
    assert (
        await c.service.get_facts(c.tenant, c.need_id, actor_id=c.actor_id)
    ).unit is None


@pytest.mark.parametrize(
    "path,encoded",
    [
        (["value"], "true"),
        (["value"], '"500"'),
        (["value"], "500.5"),
        (["provenance", "extracted_at"], '"2026-08-28T00:00:00"'),
        (["provenance", "extracted_at"], "123"),
        (["provenance", "extracted_by"], "123"),
        (["provenance", "confirmed_by"], '""'),
        (["provenance", "confirmed_by"], '" "'),
        (["provenance", "confirmed_by"], '" emp"'),
        (["provenance", "confirmed_by"], '"emp "'),
        (["provenance", "confirmed_by"], json.dumps("x" * 41)),
        (["provenance", "confirmed_by"], json.dumps("em\np")),
        (["provenance", "confirmed_by"], json.dumps("em\x7fp")),
    ],
)
async def test_corrupt_quantity_is_not_coerced(
    unit_db_case: UnitDbCase, path: list[str], encoded: str
) -> None:
    c = unit_db_case
    async with c.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE validated_needs SET quantity=jsonb_set(quantity,CAST(:path AS text[]),"
                "CAST(:bad AS jsonb)) WHERE tenant_id=:tenant AND need_id=:need"
            ),
            {"tenant": c.tenant, "need": c.need_id, "path": path, "bad": encoded},
        )
    with pytest.raises(NeedUnitUnavailableError) as caught:
        await c.service.get_facts(c.tenant, c.need_id, actor_id=c.actor_id)
    assert caught.value.code == "facts_corrupt"
    with pytest.raises(NeedUnitUnavailableError) as caught:
        await c.confirm()
    assert caught.value.code == "facts_corrupt"
    assert c.reader.calls == 0
    assert await c.confirmation_count() == await c.unit_history_count() == 0


async def test_repository_tenant_mismatch_audited_and_hidden(
    unit_db_case, caplog
) -> None:
    c = unit_db_case
    async with c.factory(c.tenant) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.units.read_facts(TenantId("other"), c.need_id)
    assert "跨租户" in caplog.text


async def test_lock_timeout_redacted(unit_db_case) -> None:
    c = unit_db_case
    async with c.sessions.begin() as session:
        await session.execute(
            select(ValidatedNeedRow)
            .where(
                ValidatedNeedRow.tenant_id == c.tenant,
                ValidatedNeedRow.need_id == c.need_id,
            )
            .with_for_update()
        )
        with pytest.raises(NeedUnitUnavailableError) as caught:
            await c.confirm()
        assert caught.value.code == "lock_timeout"
        assert "SELECT" not in str(caught.value)
    assert await c.confirmation_count() == 0


async def test_committed_but_lost_response_recovers_after_service_restart(
    unit_db_case, monkeypatch
) -> None:
    """提交后模拟连接结果丢失；不换键也不重写时间。"""
    c = unit_db_case
    original = AsyncSession.commit

    async def lost_reply(session: AsyncSession) -> None:
        """真实提交后只在本次写事务丢回包，不干扰只读事务。"""
        written = session.info.get("unit_test_written", False)
        await original(session)
        if written:
            raise OSError("sensitive-commit-response-lost")

    repo = importlib.import_module(
        "infra.db.repositories.need_units"
    ).NeedUnitRepositoryImpl
    append = repo.append_unit_history

    async def mark_written(self, *args) -> None:
        await append(self, *args)
        self._session.info["unit_test_written"] = True

    monkeypatch.setattr(repo, "append_unit_history", mark_written)
    monkeypatch.setattr(AsyncSession, "commit", lost_reply)
    with pytest.raises(NeedUnitUnavailableError) as caught:
        await c.confirm()
    assert caught.value.code == "storage_unknown"
    assert "sensitive" not in str(caught.value)
    assert await c.confirmation_count() == await c.unit_history_count() == 1
    implementation = importlib.import_module(
        "domains.demand.unit_service"
    ).NeedUnitServiceImpl
    restarted = implementation(c.factory, c.reader.access, c.reader, now=lambda: NOW)
    replay = await restarted.confirm(
        c.tenant, c.need_id, c.command, actor_id=c.actor_id, idempotency_key="first"
    )
    assert replay.confirmed_at == NOW and c.reader.calls == 1
    c.reader.denied = True
    with pytest.raises(NeedUnitPermissionError):
        await restarted.get_confirmation(
            c.tenant, c.need_id, replay.confirmation_id, actor_id=c.actor_id
        )


async def test_confirm_lock_blocks_old_quantity_update_until_commit(
    unit_db_case, monkeypatch
) -> None:
    """真实不同连接：确认先锁，旧更新等待，随后旧单位自然stale。"""
    c = unit_db_case
    repo = importlib.import_module(
        "infra.db.repositories.need_units"
    ).NeedUnitRepositoryImpl
    old_repo = importlib.import_module(
        "infra.db.repositories.need_hypotheses"
    ).ValidatedNeedRepositoryImpl
    original_apply, original_get = repo.apply_current_unit, old_repo.get_for_update
    locked, release = asyncio.Event(), asyncio.Event()
    update_pid: asyncio.Queue[int] = asyncio.Queue()

    async def hold(self, *args) -> None:
        locked.set()
        await release.wait()
        await original_apply(self, *args)

    async def observe_update(self, *args):
        await update_pid.put(
            await self._session.scalar(text("SELECT pg_backend_pid()"))
        )
        return await original_get(self, *args)

    monkeypatch.setattr(repo, "apply_current_unit", hold)
    monkeypatch.setattr(old_repo, "get_for_update", observe_update)
    confirmation = asyncio.create_task(c.confirm())
    await asyncio.wait_for(locked.wait(), 3)
    quantity_update = asyncio.create_task(c.change_quantity())
    try:
        pid = await asyncio.wait_for(update_pid.get(), 3)
        async with asyncio.timeout(3), c.sessions() as observer:
            while True:
                assert not quantity_update.done(), "旧更新未等待Need锁"
                waiting = await observer.scalar(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE pid=:pid AND datname=current_database()"
                    ),
                    {"pid": pid},
                )
                if waiting == "Lock":
                    break
    finally:
        release.set()
    first, _ = await asyncio.gather(confirmation, quantity_update)
    facts = await c.service.get_facts(c.tenant, c.need_id, actor_id=c.actor_id)
    assert (
        facts.unit_confirmation_id == first.confirmation_id
        and facts.quantity.value == 600
    )
    with pytest.raises(NeedUnitError) as caught:
        require_current_unit(facts)
    assert caught.value.code == "unit_stale"


async def test_cross_tenant_and_cross_need_foreign_keys_reject(unit_db_case) -> None:
    """不能借存在的外租户Need、原件或确认ID建立引用。"""
    c = unit_db_case
    first = await c.confirm()
    row_type = importlib.import_module("infra.db.tables").NeedUnitConfirmationRow
    other = TenantId(new_id("tn"))
    async with c.sessions.begin() as session:
        session.add(
            ValidatedNeedRow(
                tenant_id=other,
                need_id="other_need",
                account_id="acct_other",
                source_message_id="msg_other",
                created_at=NOW,
                product_category=first.model_dump(mode="json")["unit"],
            )
        )
    base = {
        "tenant_id": other,
        "need_id": "other_need",
        "confirmation_id": "nuc_other",
        "artifact_id": first.source.artifact_id,
        "source_message_id": "msg_other",
        "confirmed_by": c.actor_id,
        "confirmed_at": NOW,
        "quantity_fact_hash": first.quantity_fact_hash,
        "idempotency_key": "other",
        "request_hash": "a" * 64,
        "payload": first.model_dump(mode="json"),
    }
    with pytest.raises(DBAPIError) as foreign_artifact:
        async with c.sessions.begin() as session:
            session.add(row_type(**base))
    assert foreign_artifact.value.orig.sqlstate == "23503"
    with pytest.raises(DBAPIError) as foreign_need:
        async with c.sessions.begin() as session:
            session.add(row_type(**{**base, "tenant_id": c.tenant}))
    assert foreign_need.value.orig.sqlstate == "23503"
    with pytest.raises(DBAPIError):
        async with c.sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE validated_needs SET unit=CAST(:unit AS jsonb),unit_quantity_fact_hash=:hash,"
                    "unit_confirmation_id=:confirmation WHERE tenant_id=:tenant AND need_id=:need"
                ),
                {
                    "unit": json.dumps(first.model_dump(mode="json")["unit"]),
                    "hash": first.quantity_fact_hash,
                    "confirmation": first.confirmation_id,
                    "tenant": other,
                    "need": "other_need",
                },
            )
    async with c.factory(other) as uow:
        assert await uow.units.read_facts(other, c.need_id) is None
        assert (
            await uow.units.get_confirmation(other, c.need_id, first.confirmation_id)
            is None
        )
