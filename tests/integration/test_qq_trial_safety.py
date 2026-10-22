"""合成 PG 账本的到期、别名证据、RLS 和禁止回退；不访问外部服务。"""

import asyncio
import importlib
from dataclasses import replace
from datetime import timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.qq_alias import provision_trial, record_verified_alias
from infra.db.qq_trial_tables import QQTrialRow
from infra.db.repositories.email_inbound import InboundStore
from infra.db.session import create_engine_from
from shared.schemas.identifiers import new_id
from shared.schemas.qq_trial import QQTrialStopped, QQTrialWatermark
from tests.integration.test_qq_trial_ledger import (
    received,
    snapshot,
)
from tests.integration.test_qq_trial_ledger import (
    trial as trial,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.runtime_database_fixtures import runtime_database_scope


@pytest.mark.parametrize(
    "trial", [{"limits": {"model_timeout_seconds": 1}}], indirect=True
)
async def test_model_timeout_is_durable_across_waits(trial):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    await gate.reserve("classifications")
    await asyncio.sleep(1.05)
    with pytest.raises(QQTrialStopped):
        await gate.reserve("provider_requests", input_bytes=1)
    assert (await snapshot(gate, sessions)).provider_requests == 0


async def test_expired_scope_stops_at_db_clock_even_if_authorization_was_slow(
    trial, monkeypatch
):
    gate, sessions, *_ = trial
    # 模拟返回的数据库时钟；只替换 SELECT clock_timestamp，数据库约束/行锁仍真实。
    from sqlalchemy.ext.asyncio import AsyncSession

    real_scalar = AsyncSession.scalar
    ticks = 0

    async def clock(session, statement, *args, **kwargs):
        nonlocal ticks
        if str(statement) == "SELECT clock_timestamp() AS clock_timestamp_1":
            ticks += 1
            return (
                gate.scope.starts_at + timedelta(seconds=1)
                if ticks == 1
                else gate.scope.expires_at
            )
        return await real_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", clock)
    with pytest.raises(QQTrialStopped):
        await gate.claim_scan()
    assert ticks == 2
    assert (await snapshot(gate, sessions)).sessions == 0


@pytest.mark.parametrize(
    "field,value", [("sessions", 0), ("watermark", None), ("scope", {})]
)
async def test_database_rejects_budget_or_start_reset(trial, field, value):
    gate, sessions, *_ = trial
    assert await gate.claim_scan()
    await gate.reserve("sessions")
    await gate.initialize(QQTrialWatermark(uidvalidity=7, high_uid=10))
    with pytest.raises(DBAPIError):
        async with sessions.begin() as s:
            await s.execute(
                update(QQTrialRow)
                .where(
                    QQTrialRow.tenant_id == gate.scope.route.tenant_id,
                    QQTrialRow.trial_id == gate.scope.trial_id,
                )
                .values(**{field: value})
            )


@pytest.mark.parametrize("change", ["tenant", "tool", "artifact", "bytes"])
async def test_alias_provenance_cannot_be_forged_or_cross_tenant(trial, change):
    gate, sessions, _, _, _, proof, artifact, call = trial
    scope = gate.scope
    if change == "tenant":
        scope = scope.model_copy(
            update={"route": scope.route.model_copy(update={"tenant_id": new_id("tn")})}
        )
    if change == "tool":
        call = new_id("tcl")
    if change == "artifact":
        artifact = new_id("art")
    if change == "bytes":
        proof = replace(
            proof, raw_headers=proof.raw_headers.replace(b"Idempotency", b"Unrelated")
        )
    with pytest.raises(QQTrialStopped):
        await record_verified_alias(
            sessions,
            scope,
            proof,
            artifact_id=artifact,
            tool_call_id=call,
            authorize=gate.authorize,
        )


async def test_existing_native_cursor_is_never_reset_or_reused(trial):
    from connectors.qq_mail.inbound import initial_cursor

    gate, sessions, *_ = trial
    scope = gate.scope
    store = InboundStore(
        sessions,
        scope.route.tenant_id,
        scope.route.mailbox_alias,
        now=lambda: scope.starts_at,
    )
    original = await store.bind(
        scope.route,
        initial_cursor(scope.route, attempt_id=scope.attempt_id),
        scope.starts_at,
        int(scope.starts_at.timestamp()),
        scope.employee_id,
    )
    with pytest.raises(QQTrialStopped):
        await provision_trial(sessions, scope, gate.authorize)
    assert await store.read_cursor() == original


async def test_rls_with_real_unprivileged_role_and_nonempty_downgrade(trial, db_url, record_property):
    gate, sessions, *_ = trial
    role = "qq_trial_" + new_id("test").split("_")[1].lower()
    async with sessions.begin() as s:
        await s.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        await s.execute(
            text(
                f"GRANT SELECT, UPDATE ON qq_reply_trials, qq_sent_aliases, qq_reply_trial_events TO {role}"
            )
        )
    async with sessions.begin() as s:
        await s.execute(text(f"SET LOCAL ROLE {role}"))
        await s.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": new_id("tn")},
        )
        # 此处故意无 tenant WHERE，以真实数据库 RLS 证明隔离。
        assert (await s.scalars(select(QQTrialRow))).all() == []
        await s.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": gate.scope.route.tenant_id},
        )
        # 0080 已改为固定企业角色；任意临时角色不能通过 GUC 取得数据权限。
        assert (await s.scalars(select(QQTrialRow))).all() == []
    other_tenant = new_id("tn")
    async with runtime_database_scope(db_url) as provision:
        for actual_tenant, expected_count in ((gate.scope.route.tenant_id, 1), (other_tenant, 0)):
            engine = create_engine_from(await provision(actual_tenant))
            runtime_sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with runtime_sessions.begin() as s:
                    # 故意伪造另一企业 GUC 且无 WHERE，固定角色仍只看自己企业。
                    await s.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                        {"tenant": other_tenant if expected_count else gate.scope.route.tenant_id})
                    visible = (await s.scalars(select(QQTrialRow))).all()
                    assert len(visible) == expected_count
                    if visible:
                        assert visible[0].trial_id == gate.scope.trial_id
            finally:
                await engine.dispose()
    module = importlib.import_module("migrations.versions.0079_qq_bounded_trial")

    def downgrade(conn):
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()

    with pytest.raises(RuntimeError, match="非空"):
        async with sessions.begin() as s:
            await (await s.connection()).run_sync(downgrade)
    assert (await snapshot(gate, sessions)).trial_id == gate.scope.trial_id
    record_property("rls_fixed_roles", "own_one_other_zero_forged_guc_ignored")
    record_property("rls_arbitrary_role", "denied_even_with_matching_guc")
    record_property("rls_nonempty_downgrade", "rejected_original_evidence_retained")


async def test_concurrent_provider_reservation_and_unknown_output(trial):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    await gate.reserve("classifications")
    result = await asyncio.gather(
        gate.reserve("provider_requests", input_bytes=8),
        gate.reserve("provider_requests", input_bytes=8),
        return_exceptions=True,
    )
    assert sum(r is None for r in result) == 1
    assert sum(isinstance(r, QQTrialStopped) for r in result) == 1
    with pytest.raises(QQTrialStopped):
        await gate.provider_finished(None, 50)
    row = await snapshot(gate, sessions)
    assert row.provider_pending and row.provider_requests == 1 and row.input_bytes == 8
