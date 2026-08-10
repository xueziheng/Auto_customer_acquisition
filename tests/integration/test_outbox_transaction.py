"""S2-6 outbox 事务集成测试（PostgresEventBus + SqlAlchemyOpportunityUnitOfWork）。

行为断言，不依赖实现细节：
- ``publish`` 按计划 P1 精确落库 outbox_events：tenant_id/event_type/event_payload/
  trace_id/run_id/occurred_at/published_at/status/attempt/event_id。
- ``subscribe`` 仅内存注册；publish 只写 outbox，不触发订阅方（投递轮询延后）。
- bus 写入的行同样受 0002 guard：仅 status/delivered_at 可更新。
- 未注册事件发布 → ``ValidationError`` 且无 outbox 写入。
- UoW 同事务原子性（真实容器，非 mock）：commit 后业务行+outbox 行同时存在；
  异常自动 rollback 后二者都不存在；事件租户与 UoW 租户不一致 → publish 拒绝
  （失败关闭），业务仍可提交、无 outbox 写入。

RED 阶段仓储/总线经 importlib 延迟导入（ModuleNotFoundError/AttributeError 转行为
失败，非收集错误）。函数级本地引擎会话，避免 session 级 async fixture 的事件循环
teardown 问题。禁止打印/记录任何连接串/凭证。
"""
from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from infra.db.tables import OpportunityRow, OutboxEventRow
from shared.errors import ValidationError
from shared.events.catalog import (
    NeedValidated,
    OpportunityWon,
    ReputationThresholdBreached,
    SendingIdentityActivated,
    SendingIdentitySuspended,
    SendingIdentityThrottled,
)
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    RunId,
    TenantId,
    ValidatedNeedId,
)

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)

_MODULE_BY_SYMBOL = {
    "PostgresEventBus": "infra.db.outbox",
    "SqlAlchemyOpportunityUnitOfWork": "infra.db.unit_of_work",
    "Opportunity": "domains.opportunities.models",
}


def _opp(opp_id: str, tenant_id: str, need_id: str):
    """构造最小合法机会（UoW 业务写入口）。"""
    Opportunity = _load("Opportunity")
    return Opportunity(
        opportunity_id=OpportunityId(opp_id),
        tenant_id=TenantId(tenant_id),
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId(need_id),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
    )


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED 阶段未建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    """函数级本地引擎（bus/UoW 测试用），结束后 dispose。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


class _Recorder:
    """内存订阅记录器（EventHandler Protocol：handle 方法）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, event: object) -> None:
        self.calls.append(type(event).__name__)


async def test_bus_publish_inserts_outbox_row(engine_fx: AsyncEngine) -> None:
    """publish 精确落库 outbox_events 各字段。"""
    PostgresEventBus = _load("PostgresEventBus")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    session = sf()
    try:
        bus = PostgresEventBus(session, TenantId("tBus1"), now=lambda: _NOW)
        await bus.publish(
            OpportunityWon(
                tenant_id=TenantId("tBus1"),
                occurred_at=_NOW,
                run_id=RunId("r1"),
                opportunity_id=OpportunityId("opp-bus-1"),
                closed_by=EmployeeId("e1"),
            )
        )
        await session.commit()

        verify = sf()
        try:
            row = (
                await verify.execute(
                    select(OutboxEventRow).where(OutboxEventRow.tenant_id == "tBus1")
                )
            ).scalar_one()
        finally:
            await verify.close()

        assert row.tenant_id == "tBus1"
        assert row.event_type == "OpportunityWon"
        assert row.attempt == 1
        assert row.run_id == "r1"
        assert row.occurred_at == _NOW
        assert row.published_at == _NOW
        assert row.status == "pending"
        assert row.trace_id.startswith("trc_")
        assert row.event_id.startswith("evt_")
        assert row.event_payload["opportunity_id"] == "opp-bus-1"
        assert row.event_payload["closed_by"] == "e1"
    finally:
        await session.close()


async def test_bus_subscribe_in_memory_no_dispatch(engine_fx: AsyncEngine) -> None:
    """subscribe 仅内存注册；publish 只写 outbox，不触发订阅方（投递轮询延后）。"""
    PostgresEventBus = _load("PostgresEventBus")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    session = sf()
    try:
        bus = PostgresEventBus(session, TenantId("tBus2"))
        recorder = _Recorder()
        bus.subscribe(OpportunityWon, recorder)
        await bus.publish(
            OpportunityWon(
                tenant_id=TenantId("tBus2"), occurred_at=_NOW, run_id=None,
                opportunity_id=OpportunityId("opp-bus-2"), closed_by=EmployeeId("e1"),
            )
        )
        await session.commit()
        assert recorder.calls == []  # publish 不投递；投递由后续轮询负责
    finally:
        await session.close()


async def test_outbox_guard_enforced_on_bus_rows(engine_fx: AsyncEngine) -> None:
    """bus 写入的行同样受 0002 guard：仅 status/delivered_at 可更新。"""
    PostgresEventBus = _load("PostgresEventBus")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    session = sf()
    try:
        bus = PostgresEventBus(session, TenantId("tBus3"))
        await bus.publish(
            OpportunityWon(
                tenant_id=TenantId("tBus3"), occurred_at=_NOW, run_id=RunId("r3"),
                opportunity_id=OpportunityId("opp-bus-3"), closed_by=EmployeeId("e1"),
            )
        )
        await session.commit()

        async with engine_fx.connect() as conn:
            try:
                await conn.execute(
                    text(
                        "UPDATE outbox_events SET event_type = :t WHERE tenant_id = :tn"
                    ),
                    {"t": "HandoffRequested", "tn": "tBus3"},
                )
            except DBAPIError:
                await conn.rollback()
            else:
                await conn.rollback()
                pytest.fail("event_type 更新应被 outbox guard 拒绝")

        async with engine_fx.connect() as conn:
            await conn.execute(
                text(
                    "UPDATE outbox_events SET status = 'delivered', delivered_at = now() "
                    "WHERE tenant_id = :tn"
                ),
                {"tn": "tBus3"},
            )
    finally:
        await session.close()


async def test_bus_publish_unknown_event_rejected(engine_fx: AsyncEngine) -> None:
    """未注册事件（catalog 有但不在白名单）→ ValidationError 且无 outbox 写入。"""
    PostgresEventBus = _load("PostgresEventBus")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    session = sf()
    try:
        bus = PostgresEventBus(session, TenantId("tBus4"))
        evt = NeedValidated(
            tenant_id=TenantId("tBus4"), occurred_at=_NOW, run_id=RunId("r4"),
            need_id=ValidatedNeedId("n1"), account_id=None, category="hinges",
            evidence_level=None, completeness=3,
        )
        with pytest.raises(ValidationError):
            await bus.publish(evt)
        await session.commit()

        verify = sf()
        try:
            rows = (
                await verify.execute(
                    select(OutboxEventRow).where(OutboxEventRow.tenant_id == "tBus4")
                )
            ).scalars().all()
        finally:
            await verify.close()
        assert rows == []
    finally:
        await session.close()


# --- Batch 3：SqlAlchemyOpportunityUnitOfWork 同事务原子性 ----------------------


async def test_uow_commit_business_and_outbox_together(engine_fx: AsyncEngine) -> None:
    """commit 后业务行 + outbox 行同时存在（同事务原子性，非 mock）。"""
    SqlAlchemyOpportunityUnitOfWork = _load("SqlAlchemyOpportunityUnitOfWork")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    async with SqlAlchemyOpportunityUnitOfWork(sf, TenantId("tUowC")) as uow:
        await uow.opportunities.add(_opp("opp-uow-c", "tUowC", "need-uow-c"))
        await uow.bus.publish(
            OpportunityWon(
                tenant_id=TenantId("tUowC"), occurred_at=_NOW, run_id=RunId("rC"),
                opportunity_id=OpportunityId("opp-uow-c"), closed_by=EmployeeId("e1"),
            )
        )

    verify = sf()
    try:
        outbox_rows = (
            await verify.execute(
                select(OutboxEventRow).where(OutboxEventRow.tenant_id == "tUowC")
            )
        ).scalars().all()
        opp_rows = (
            await verify.execute(
                select(OpportunityRow).where(OpportunityRow.tenant_id == "tUowC")
            )
        ).scalars().all()
    finally:
        await verify.close()
    assert len(outbox_rows) == 1
    assert len(opp_rows) == 1


async def test_uow_rollback_on_exception(engine_fx: AsyncEngine) -> None:
    """UoW 内异常 → 自动 rollback：业务行 + outbox 行都不存在。"""
    SqlAlchemyOpportunityUnitOfWork = _load("SqlAlchemyOpportunityUnitOfWork")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    with pytest.raises(RuntimeError):
        async with SqlAlchemyOpportunityUnitOfWork(sf, TenantId("tUowR")) as uow:
            await uow.opportunities.add(_opp("opp-uow-r", "tUowR", "need-uow-r"))
            await uow.bus.publish(
                OpportunityWon(
                    tenant_id=TenantId("tUowR"), occurred_at=_NOW, run_id=RunId("rR"),
                    opportunity_id=OpportunityId("opp-uow-r"), closed_by=EmployeeId("e1"),
                )
            )
            raise RuntimeError("boom")

    verify = sf()
    try:
        outbox_rows = (
            await verify.execute(
                select(OutboxEventRow).where(OutboxEventRow.tenant_id == "tUowR")
            )
        ).scalars().all()
        opp_rows = (
            await verify.execute(
                select(OpportunityRow).where(OpportunityRow.tenant_id == "tUowR")
            )
        ).scalars().all()
    finally:
        await verify.close()
    assert outbox_rows == []
    assert opp_rows == []


async def test_uow_publish_tenant_mismatch_rejected(engine_fx: AsyncEngine) -> None:
    """事件租户与 UoW 租户不一致 → publish 拒绝（失败关闭），业务仍可提交。"""
    SqlAlchemyOpportunityUnitOfWork = _load("SqlAlchemyOpportunityUnitOfWork")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    async with SqlAlchemyOpportunityUnitOfWork(sf, TenantId("tUowT")) as uow:
        await uow.opportunities.add(_opp("opp-uow-t", "tUowT", "need-uow-t"))
        with pytest.raises(ValueError):
            await uow.bus.publish(
                OpportunityWon(
                    tenant_id=TenantId("tOther"), occurred_at=_NOW, run_id=RunId("rT"),
                    opportunity_id=OpportunityId("opp-uow-t"), closed_by=EmployeeId("e1"),
                )
            )

    verify = sf()
    try:
        outbox_rows = (
            await verify.execute(
                select(OutboxEventRow).where(OutboxEventRow.tenant_id == "tUowT")
            )
        ).scalars().all()
        opp_rows = (
            await verify.execute(
                select(OpportunityRow).where(OpportunityRow.tenant_id == "tUowT")
            )
        ).scalars().all()
    finally:
        await verify.close()
    assert outbox_rows == []
    assert len(opp_rows) == 1  # 业务照常提交；跨租户事件被拒、无 outbox 写入


@pytest.mark.parametrize(
    "event",
    [
        SendingIdentityActivated(
            tenant_id=TenantId("tSendingEvent"), occurred_at=_NOW, run_id=RunId("run-si"),
            sending_identity_id="sid-event-1",
        ),
        SendingIdentityThrottled(
            tenant_id=TenantId("tSendingEvent"), occurred_at=_NOW, run_id=None,
            sending_identity_id="sid-event-2", new_state="throttled",
            trigger_metric="hard_bounce_rate", metric_value="0.031000",
        ),
        SendingIdentitySuspended(
            tenant_id=TenantId("tSendingEvent"), occurred_at=_NOW, run_id=None,
            sending_identity_id="sid-event-3", reason="spam_trap",
        ),
        ReputationThresholdBreached(
            tenant_id=TenantId("tSendingEvent"), occurred_at=_NOW, run_id=None,
            sending_identity_id="sid-event-4", metric="complaint_rate",
            value="0.001100", threshold="0.001000", severity="watch",
        ),
    ],
)
async def test_sending_identity_events_are_registered_roundtrip_and_safe(event: object) -> None:
    """四个 sending identity 事件显式白名单 roundtrip，payload 不含敏感字段。"""
    from infra.db.outbox import deserialize, resolve_event_type, serialize

    event_type = resolve_event_type(type(event).__name__)
    payload = serialize(event)  # type: ignore[arg-type]
    assert deserialize(event_type, payload) == event
    assert {"address", "domain", "connector_ref", "note"}.isdisjoint(payload)
