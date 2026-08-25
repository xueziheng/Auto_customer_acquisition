"""S3-10 human_handoff 集成：0007 round-trip/只增与真实 Postgres 状态机。"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
    ScopeLevel,
)
from shared.errors import TransientError
from shared.events.catalog import HandoffAccepted, HandoffRequested
from shared.schemas.identifiers import EmployeeId, HandoffId, OpportunityId, TenantId

_ROOT = Path(__file__).resolve().parents[2]
_BASE = datetime(2026, 8, 9, 1, 0, 0, tzinfo=UTC)


def _run_alembic(db_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *args],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} 失败（不输出连接内容）"


def _sync_columns(conn, table: str) -> set[str]:
    return {str(c["name"]) for c in inspect(conn).get_columns(table)}


def _sync_unique_names(conn, table: str) -> set[str]:
    return {str(c["name"]) for c in inspect(conn).get_unique_constraints(table)}


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(_sync_columns, table)


async def _unique_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(_sync_unique_names, table)


async def test_0007_roundtrip_exact_schema_and_append_only(db_url: str) -> None:
    """0007→0006→head 精确 round-trip；新审计表 UPDATE/DELETE 被 guard 拒绝。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        assert await _columns(engine, "handoff_escalations") == {
            "escalation_id",
            "tenant_id",
            "handoff_id",
            "level",
            "escalated_at",
            "note",
        }
        assert "uq_handoffs_tenant_handoff" in await _unique_names(engine, "handoffs")
        assert (
            "uq_handoff_escalations_tenant_handoff_level"
            in await _unique_names(engine, "handoff_escalations")
        )

        _run_alembic(db_url, "downgrade", "0006")
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        assert "handoff_escalations" not in tables
        assert "uq_handoffs_tenant_handoff" not in await _unique_names(
            engine, "handoffs"
        )
        _run_alembic(db_url, "upgrade", "head")

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO opportunities (opportunity_id, tenant_id, account_id, "
                    "account_name, country, need_id, product_category) VALUES "
                    "('opp-0007', 't0007', 'acc-1', 'Acme', 'US', 'need-0007', 'hinges')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO handoffs (handoff_id, tenant_id, opportunity_id, trigger, "
                    "requested_at, account_name, country, why_valuable, customer_verbatim) "
                    "VALUES ('hand-0007', 't0007', 'opp-0007', 'quote_requested', now(), "
                    "'Acme', 'US', 'expansion', 'we need hinges')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO handoff_escalations "
                    "(escalation_id, tenant_id, handoff_id, level, escalated_at) "
                    "VALUES ('esc-0007', 't0007', 'hand-0007', 1, now())"
                )
            )

        for sql in (
            "UPDATE handoff_escalations SET level = 2 WHERE escalation_id = 'esc-0007'",
            "DELETE FROM handoff_escalations WHERE escalation_id = 'esc-0007'",
        ):
            async with engine.connect() as conn:
                with pytest.raises(DBAPIError):
                    await conn.execute(text(sql))
                    await conn.commit()
                await conn.rollback()
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0007_unique_and_composite_fk(db_url: str) -> None:
    """同 level 重复被唯一约束拒绝；跨租户 handoff 引用被复合 FK 拒绝。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            for tenant, suffix in (("t7A", "a"), ("t7B", "b")):
                await conn.execute(
                    text(
                        "INSERT INTO opportunities (opportunity_id, tenant_id, account_id, "
                        "account_name, country, need_id, product_category) VALUES "
                        "(:opp, :tenant, 'acc', 'Acme', 'US', :need, 'hinges')"
                    ),
                    {"opp": f"opp-7-{suffix}", "tenant": tenant, "need": f"need-7-{suffix}"},
                )
                await conn.execute(
                    text(
                        "INSERT INTO handoffs (handoff_id, tenant_id, opportunity_id, trigger, "
                        "requested_at, account_name, country, why_valuable, customer_verbatim) "
                        "VALUES (:handoff, :tenant, :opp, 'quote_requested', now(), "
                        "'Acme', 'US', 'expansion', 'we need hinges')"
                    ),
                    {"handoff": f"hand-7-{suffix}", "tenant": tenant, "opp": f"opp-7-{suffix}"},
                )
            await conn.execute(
                text(
                    "INSERT INTO handoff_escalations "
                    "(escalation_id, tenant_id, handoff_id, level, escalated_at) "
                    "VALUES ('esc-7-a', 't7A', 'hand-7-a', 1, now())"
                )
            )
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO handoff_escalations "
                        "(escalation_id, tenant_id, handoff_id, level, escalated_at) "
                        "VALUES ('esc-7-dup', 't7A', 'hand-7-a', 1, now())"
                    )
                )
                await conn.commit()
            await conn.rollback()
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO handoff_escalations "
                        "(escalation_id, tenant_id, handoff_id, level, escalated_at) "
                        "VALUES ('esc-7-cross', 't7A', 'hand-7-b', 2, now())"
                    )
                )
                await conn.commit()
            await conn.rollback()
    finally:
        await engine.dispose()


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class _OpportunityService:
    def __init__(self) -> None:
        self.records: list[tuple] = []

    async def record_handoff_escalation(
        self, tenant_id, handoff_id, level, at, *, actor
    ) -> None:
        self.records.append((tenant_id, handoff_id, level, at, actor))


class _EmployeeService:
    def __init__(
        self, tenant: TenantId | None = None
    ) -> None:
        tenant = tenant or TenantId("tFlowIntegration")
        self.rows = {
            EmployeeId("sales"): EmployeeView(
                EmployeeId("sales"), tenant, "Sales", "sales", manager_id=EmployeeId("manager")
            ),
            EmployeeId("manager"): EmployeeView(
                EmployeeId("manager"), tenant, "Manager", "manager", manager_id=EmployeeId("boss")
            ),
            EmployeeId("boss"): EmployeeView(
                EmployeeId("boss"), tenant, "Boss", "boss"
            ),
        }

    async def get_employee(self, tenant_id, employee_id, *, actor):
        return self.rows[employee_id]


class _Notifier:
    def __init__(self) -> None:
        self.rows = []

    async def notify(self, notice) -> None:
        if notice.dedup_key not in {row.dedup_key for row in self.rows}:
            self.rows.append(notice)


class _OwnerRetryNotifier(_Notifier):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def notify(self, notice) -> None:
        if notice.level == "owner":
            self.attempts += 1
            if self.attempts == 1:
                raise TransientError("temporary owner notification failure")
        await super().notify(notice)


class _ManagerRetryEmployeeService(_EmployeeService):
    def __init__(self, tenant: TenantId) -> None:
        super().__init__(tenant)
        self.failed = False

    async def get_employee(self, tenant_id, employee_id, *, actor):
        if employee_id == EmployeeId("manager") and not self.failed:
            self.failed = True
            raise TransientError("temporary manager lookup failure")
        return await super().get_employee(tenant_id, employee_id, actor=actor)


class _BossReminderRetryNotifier(_Notifier):
    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[tuple[str, str]] = []
        self.failed = False

    async def notify(self, notice) -> None:
        self.attempts.append((notice.level, notice.dedup_key))
        if notice.level == "boss_reminder" and not self.failed:
            self.failed = True
            raise TransientError("crash after boss reminder delivery")
        await super().notify(notice)


class _Registry:
    def __init__(self) -> None:
        self.rows = []

    def register_handler(self, event_type, handler_name, handler) -> None:
        self.rows.append((event_type, handler_name, handler))


def _step_handlers(
    flow,
    *,
    tenant: TenantId,
    clock: _Clock,
    opportunities: _OpportunityService,
    notifier: _Notifier,
    employees=None,
    t1: timedelta,
    t2: timedelta,
):
    return flow.build_human_handoff_step_handlers(
        opportunity_service=opportunities,
        employee_service=employees or _EmployeeService(tenant),
        notifier=notifier,
        opportunity_system_actor=OpportunityActor(
            "system:handoff", OpportunityScope(level=ScopeLevel.SYSTEM), "system"
        ),
        employee_system_actor=EmployeeActor(
            "system:handoff", EmployeeScope.SYSTEM, "system"
        ),
        t1=t1,
        t2=t2,
        now=clock.now,
    )


def _flow_runtime(
    db_url: str,
    flow,
    *,
    tenant: TenantId,
    clock: _Clock,
    opportunities: _OpportunityService,
    notifier: _Notifier,
    employees=None,
    t1: timedelta,
    t2: timedelta,
):
    from infra.db.session import create_engine_from
    from infra.db.workflow_engine import PostgresWorkflowEngine

    db = create_engine_from(db_url)
    factory = async_sessionmaker(bind=db, expire_on_commit=False)
    engine = PostgresWorkflowEngine(
        factory,
        _step_handlers(
            flow,
            tenant=tenant,
            clock=clock,
            opportunities=opportunities,
            notifier=notifier,
            employees=employees,
            t1=t1,
            t2=t2,
        ),
        now=clock.now,
    )
    registry = _Registry()
    flow.register_human_handoff(engine, registry, t1=t1, t2=t2)
    return db, engine, registry


async def _drive_to_boss_wait(
    *, engine, requested, tenant: TenantId, handoff: str, clock: _Clock, t1, t2
) -> None:
    await requested.handle(
        HandoffRequested(
            tenant_id=tenant,
            occurred_at=_BASE,
            handoff_id=HandoffId(handoff),
            opportunity_id=OpportunityId(f"opp-{handoff}"),
            assigned_to=EmployeeId("sales"),
            trigger="quote_requested",
        )
    )
    assert await engine.poll_due(tenant, 1) == 1
    clock.value = _BASE + t1
    assert await engine.poll_due(tenant, 1) == 1
    assert await engine.poll_due(tenant, 1) == 1
    clock.value = _BASE + t1 + t2
    assert await engine.poll_due(tenant, 1) == 1
    assert await engine.poll_due(tenant, 1) == 1


async def test_delayed_t1_t2_escalation_preserves_planned_boundaries(
    db_url: str,
) -> None:
    """延迟消费不重置 T1，延迟 T1 升级也不把 T2 再往后顺延。"""
    from infra.db.session import create_engine_from
    from infra.db.workflow_engine import PostgresWorkflowEngine

    try:
        flow = importlib.import_module("workflows.human_handoff.flow")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：human_handoff flow 尚未实现（{exc}）")
    t1 = timedelta(minutes=10)
    t2 = timedelta(minutes=20)
    clock = _Clock(_BASE + timedelta(minutes=15))
    opportunities = _OpportunityService()
    notifier = _Notifier()
    step_handlers = flow.build_human_handoff_step_handlers(
        opportunity_service=opportunities,
        employee_service=_EmployeeService(),
        notifier=notifier,
        opportunity_system_actor=OpportunityActor(
            "system:handoff", OpportunityScope(level=ScopeLevel.SYSTEM), "system"
        ),
        employee_system_actor=EmployeeActor(
            "system:handoff", EmployeeScope.SYSTEM, "system"
        ),
        t1=t1,
        t2=t2,
        now=clock.now,
    )
    db = create_engine_from(db_url)
    factory = async_sessionmaker(bind=db, expire_on_commit=False)
    engine = PostgresWorkflowEngine(factory, step_handlers, now=clock.now)
    registry = _Registry()
    try:
        flow.register_human_handoff(engine, registry, t1=t1, t2=t2)
        requested = next(row[2] for row in registry.rows if row[0] is HandoffRequested)
        tenant = TenantId("tFlowIntegration")
        event = HandoffRequested(
            tenant_id=tenant,
            occurred_at=_BASE,
            handoff_id=HandoffId("hand-flow"),
            opportunity_id=OpportunityId("opp-flow"),
            assigned_to=EmployeeId("sales"),
            trigger="quote_requested",
        )
        await requested.handle(event)
        await requested.handle(event)
        async with db.connect() as conn:
            run_count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM workflow_runs WHERE tenant_id = :tenant "
                        "AND workflow_type = 'human_handoff' AND subject_ref = 'hand-flow'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).scalar_one()
        assert run_count == 1

        assert await engine.poll_due(tenant, 1) == 1  # owner notify → T1 wait
        assert notifier.rows[0].level == "owner"
        assert notifier.rows[0].sla_due_at == _BASE + t1
        assert await engine.poll_due(tenant, 1) == 1  # T1 already expired
        assert await engine.poll_due(tenant, 1) == 1  # manager escalation → T2 wait
        assert opportunities.records[0][2] == 1
        async with db.connect() as conn:
            due = (
                await conn.execute(
                    text(
                        "SELECT due_at FROM workflow_steps WHERE tenant_id = :tenant "
                        "AND step_name = 'wait_acceptance_t2'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).scalar_one()
        assert due == _BASE + t1 + t2
        assert due != clock.now() + t2

        clock.value = _BASE + t1 + t2
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.poll_due(tenant, 1) == 1
        assert opportunities.records[1][2] == 2
        assert [row.recipient_id for row in notifier.rows] == [
            EmployeeId("sales"),
            EmployeeId("manager"),
            EmployeeId("boss"),
        ]
        assert all(row.assigned_to == EmployeeId("sales") for row in notifier.rows)
    finally:
        await db.dispose()


async def test_accepted_outbox_waits_until_workflow_is_waiting_event(
    db_url: str,
) -> None:
    """Requested/Accepted 连续到达时，Accepted 不得在 notify_owner 阶段被 ACK。"""
    from infra.db.outbox import PostgresEventBus
    from infra.db.outbox_delivery import OutboxDeliverer
    from infra.db.session import create_engine_from
    from infra.db.workflow_engine import PostgresWorkflowEngine

    flow = importlib.import_module("workflows.human_handoff.flow")
    tenant = TenantId("tFlowOutboxEarly")
    clock = _Clock(_BASE)
    opportunities = _OpportunityService()
    notifier = _Notifier()
    db = create_engine_from(db_url)
    factory = async_sessionmaker(bind=db, expire_on_commit=False)
    handlers = _step_handlers(
        flow,
        tenant=tenant,
        clock=clock,
        opportunities=opportunities,
        notifier=notifier,
        t1=timedelta(minutes=10),
        t2=timedelta(minutes=20),
    )
    engine = PostgresWorkflowEngine(factory, handlers, now=clock.now)
    deliverer = OutboxDeliverer(factory, tenant, now=clock.now)
    flow.register_human_handoff(
        engine,
        deliverer,
        t1=timedelta(minutes=10),
        t2=timedelta(minutes=20),
    )
    try:
        session = factory()
        try:
            bus = PostgresEventBus(session, tenant, now=clock.now)
            await bus.publish(
                HandoffRequested(
                    tenant_id=tenant,
                    occurred_at=_BASE,
                    handoff_id=HandoffId("hand-early"),
                    opportunity_id=OpportunityId("opp-early"),
                    assigned_to=EmployeeId("sales"),
                    trigger="quote_requested",
                )
            )
            clock.value = _BASE + timedelta(seconds=1)
            await bus.publish(
                HandoffAccepted(
                    tenant_id=tenant,
                    occurred_at=clock.now(),
                    handoff_id=HandoffId("hand-early"),
                    accepted_by=EmployeeId("sales"),
                )
            )
            await session.commit()
        finally:
            await session.close()

        await deliverer.drain()
        async with db.connect() as conn:
            deliveries = (
                await conn.execute(
                    text(
                        "SELECT e.event_type, e.status AS event_status, "
                        "d.status AS delivery_status FROM outbox_events e "
                        "JOIN outbox_deliveries d ON d.tenant_id = e.tenant_id "
                        "AND d.event_id = e.event_id WHERE e.tenant_id = :tenant "
                        "ORDER BY e.published_at"
                    ),
                    {"tenant": str(tenant)},
                )
            ).mappings().all()
        assert [row["event_type"] for row in deliveries] == [
            "HandoffRequested",
            "HandoffAccepted",
        ]
        assert deliveries[0]["delivery_status"] == "delivered"
        assert deliveries[1]["event_status"] == "pending"
        assert deliveries[1]["delivery_status"] == "pending"

        assert await engine.poll_due(tenant, 1) == 1
        clock.value = _BASE + timedelta(minutes=1)
        await deliverer.drain()
        async with db.connect() as conn:
            accepted_status = (
                await conn.execute(
                    text(
                        "SELECT e.status, d.status FROM outbox_events e "
                        "JOIN outbox_deliveries d ON d.tenant_id = e.tenant_id "
                        "AND d.event_id = e.event_id WHERE e.tenant_id = :tenant "
                        "AND e.event_type = 'HandoffAccepted'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).one()
            run_status = (
                await conn.execute(
                    text(
                        "SELECT status FROM workflow_runs WHERE tenant_id = :tenant "
                        "AND workflow_type = 'human_handoff' "
                        "AND subject_ref = 'hand-early'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).scalar_one()
        assert tuple(accepted_status) == ("delivered", "delivered")
        assert run_status == "completed"
        assert opportunities.records == []
    finally:
        await db.dispose()


async def test_terminal_accepted_repeat_uses_durable_evidence_after_restart(
    db_url: str,
) -> None:
    """流程完成后重建 engine/handler，同一 acceptance 只凭 DB 指纹 no-op。"""
    from infra.db.session import create_engine_from
    from infra.db.workflow_engine import PostgresWorkflowEngine

    flow = importlib.import_module("workflows.human_handoff.flow")
    tenant = TenantId("tFlowRestart")
    clock = _Clock(_BASE)
    opportunities = _OpportunityService()
    notifier = _Notifier()
    db = create_engine_from(db_url)
    factory = async_sessionmaker(bind=db, expire_on_commit=False)

    def _new_engine_and_registry():
        handlers = _step_handlers(
            flow,
            tenant=tenant,
            clock=clock,
            opportunities=opportunities,
            notifier=notifier,
            t1=timedelta(minutes=10),
            t2=timedelta(minutes=20),
        )
        engine = PostgresWorkflowEngine(factory, handlers, now=clock.now)
        registry = _Registry()
        flow.register_human_handoff(
            engine,
            registry,
            t1=timedelta(minutes=10),
            t2=timedelta(minutes=20),
        )
        return engine, registry

    try:
        engine1, registry1 = _new_engine_and_registry()
        requested = next(
            row[2] for row in registry1.rows if row[0] is HandoffRequested
        )
        accepted1 = next(
            row[2] for row in registry1.rows if row[0] is HandoffAccepted
        )
        request_event = HandoffRequested(
            tenant_id=tenant,
            occurred_at=_BASE,
            handoff_id=HandoffId("hand-restart"),
            opportunity_id=OpportunityId("opp-restart"),
            assigned_to=EmployeeId("sales"),
            trigger="quote_requested",
        )
        accepted_event = HandoffAccepted(
            tenant_id=tenant,
            occurred_at=_BASE + timedelta(minutes=1),
            handoff_id=HandoffId("hand-restart"),
            accepted_by=EmployeeId("sales"),
        )
        await requested.handle(request_event)
        assert await engine1.poll_due(tenant, 1) == 1
        await accepted1.handle(accepted_event)

        engine2, registry2 = _new_engine_and_registry()
        accepted_after_restart = next(
            row[2] for row in registry2.rows if row[0] is HandoffAccepted
        )
        await accepted_after_restart.handle(accepted_event)

        async with db.connect() as conn:
            run = (
                await conn.execute(
                    text(
                        "SELECT status, context FROM workflow_runs "
                        "WHERE tenant_id = :tenant AND workflow_type = 'human_handoff' "
                        "AND subject_ref = 'hand-restart'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).mappings().one()
        assert run["status"] == "completed"
        fingerprints = run["context"]["__wf_delivered_events"]
        assert len(fingerprints) == 1
        assert len(fingerprints[0]) == 64
        assert "hand-restart" not in fingerprints[0]
        assert await engine2.find_active_run(
            tenant, "human_handoff", "hand-restart"
        ) is None
        second_run = await engine2.start(
            tenant,
            "human_handoff",
            "hand-restart",
            {
                "handoff_id": "hand-restart",
                "opportunity_id": "opp-restart",
                "assigned_to": "sales",
                "sla_started_at": _BASE.isoformat(),
            },
            "restart-second-run",
            scheduled_at=_BASE,
        )
        assert not await engine2.has_delivered_event(
            tenant,
            "human_handoff",
            "hand-restart",
            "HandoffAccepted",
            {"handoff_id": "hand-restart", "accepted_by": "sales"},
        )
        with pytest.raises(TransientError):
            await accepted_after_restart.handle(accepted_event)
        assert await engine2.find_active_run(
            tenant, "human_handoff", "hand-restart"
        ) == second_run
    finally:
        await db.dispose()


async def test_owner_notification_retry_preserves_absolute_t1_boundary(
    db_url: str,
) -> None:
    """负责人通知瞬态重试只改变可领取时间，不得把绝对 T1 向后顺延。"""
    flow = importlib.import_module("workflows.human_handoff.flow")
    tenant = TenantId("tFlowOwnerRetry")
    t1 = timedelta(minutes=10)
    t2 = timedelta(minutes=20)
    clock = _Clock(_BASE)
    notifier = _OwnerRetryNotifier()
    db, engine, registry = _flow_runtime(
        db_url,
        flow,
        tenant=tenant,
        clock=clock,
        opportunities=_OpportunityService(),
        notifier=notifier,
        t1=t1,
        t2=t2,
    )
    requested = next(row[2] for row in registry.rows if row[0] is HandoffRequested)
    try:
        await requested.handle(
            HandoffRequested(
                tenant_id=tenant,
                occurred_at=_BASE,
                handoff_id=HandoffId("hand-owner-retry"),
                opportunity_id=OpportunityId("opp-owner-retry"),
                assigned_to=EmployeeId("sales"),
                trigger="quote_requested",
            )
        )
        assert await engine.poll_due(tenant, 1) == 1
        clock.value = _BASE + timedelta(seconds=30)
        assert await engine.poll_due(tenant, 1) == 1

        async with db.connect() as conn:
            due = (
                await conn.execute(
                    text(
                        "SELECT due_at FROM workflow_steps WHERE tenant_id = :tenant "
                        "AND step_name = 'wait_acceptance_t1'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).scalar_one()
        assert due == _BASE + t1
        assert notifier.attempts == 2
    finally:
        await db.dispose()


async def test_manager_escalation_retry_preserves_absolute_t2_boundary(
    db_url: str,
) -> None:
    """经理升级瞬态重试只改变可领取时间，不得把绝对 T2 向后顺延。"""
    flow = importlib.import_module("workflows.human_handoff.flow")
    tenant = TenantId("tFlowManagerRetry")
    t1 = timedelta(minutes=10)
    t2 = timedelta(minutes=20)
    clock = _Clock(_BASE)
    employees = _ManagerRetryEmployeeService(tenant)
    db, engine, registry = _flow_runtime(
        db_url,
        flow,
        tenant=tenant,
        clock=clock,
        opportunities=_OpportunityService(),
        notifier=_Notifier(),
        employees=employees,
        t1=t1,
        t2=t2,
    )
    requested = next(row[2] for row in registry.rows if row[0] is HandoffRequested)
    try:
        await requested.handle(
            HandoffRequested(
                tenant_id=tenant,
                occurred_at=_BASE,
                handoff_id=HandoffId("hand-manager-retry"),
                opportunity_id=OpportunityId("opp-manager-retry"),
                assigned_to=EmployeeId("sales"),
                trigger="quote_requested",
            )
        )
        assert await engine.poll_due(tenant, 1) == 1
        clock.value = _BASE + t1
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.poll_due(tenant, 1) == 1
        clock.value += timedelta(seconds=30)
        assert await engine.poll_due(tenant, 1) == 1

        async with db.connect() as conn:
            due = (
                await conn.execute(
                    text(
                        "SELECT due_at FROM workflow_steps WHERE tenant_id = :tenant "
                        "AND step_name = 'wait_acceptance_t2'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).scalar_one()
        assert due == _BASE + t1 + t2
        assert employees.failed is True
    finally:
        await db.dispose()


async def test_boss_reminders_retry_key_and_acceptance_race_are_durable(
    db_url: str,
) -> None:
    """两次 reminder key 不同；崩溃重试稳定，accepted 竞争后停止。"""
    flow = importlib.import_module("workflows.human_handoff.flow")
    tenant = TenantId("tFlowBossReminderRace")
    t1 = timedelta(minutes=10)
    t2 = timedelta(minutes=20)
    clock = _Clock(_BASE)
    opportunities = _OpportunityService()
    notifier = _BossReminderRetryNotifier()
    db, engine, registry = _flow_runtime(
        db_url,
        flow,
        tenant=tenant,
        clock=clock,
        opportunities=opportunities,
        notifier=notifier,
        t1=t1,
        t2=t2,
    )
    requested = next(row[2] for row in registry.rows if row[0] is HandoffRequested)
    accepted = next(row[2] for row in registry.rows if row[0] is HandoffAccepted)
    try:
        await _drive_to_boss_wait(
            engine=engine,
            requested=requested,
            tenant=tenant,
            handoff="hand-reminder-race",
            clock=clock,
            t1=t1,
            t2=t2,
        )
        clock.value = _BASE + t1 + t2 + t2
        assert await engine.poll_due(tenant, 1) == 1
        clock.value += timedelta(seconds=30)
        assert await engine.poll_due(tenant, 1) == 1
        clock.value = _BASE + t1 + t2 + t2 + t2
        assert await engine.poll_due(tenant, 1) == 1
        reminder_attempts = [
            key for level, key in notifier.attempts if level == "boss_reminder"
        ]
        assert reminder_attempts == [
            "human_handoff:hand-reminder-race:boss-reminder:1",
            "human_handoff:hand-reminder-race:boss-reminder:1",
            "human_handoff:hand-reminder-race:boss-reminder:2",
        ]
        assert [row.level for row in notifier.rows] == [
            "owner",
            "manager",
            "boss",
            "boss_reminder",
            "boss_reminder",
        ]
        assert [row.dedup_key for row in notifier.rows[-2:]] == [
            "human_handoff:hand-reminder-race:boss-reminder:1",
            "human_handoff:hand-reminder-race:boss-reminder:2",
        ]
        async with db.connect() as conn:
            boss_wait = (
                await conn.execute(
                    text(
                        "SELECT status, data FROM workflow_steps "
                        "WHERE tenant_id = :tenant "
                        "AND step_name = 'wait_acceptance_boss'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).mappings().one()
        assert boss_wait["status"] == "waiting_event"
        assert boss_wait["data"]["reminder_index"] == 3

        clock.value = _BASE + t1 + t2 + t2 + t2 + t2
        polled, _ = await asyncio.gather(
            engine.poll_due(tenant, 1),
            accepted.handle(
                HandoffAccepted(
                    tenant_id=tenant,
                    occurred_at=clock.now(),
                    handoff_id=HandoffId("hand-reminder-race"),
                    accepted_by=EmployeeId("sales"),
                )
            ),
        )
        assert polled in (0, 1)
        async with db.connect() as conn:
            run_status = (
                await conn.execute(
                    text(
                        "SELECT status FROM workflow_runs WHERE tenant_id = :tenant "
                        "AND workflow_type = 'human_handoff' "
                        "AND subject_ref = 'hand-reminder-race'"
                    ),
                    {"tenant": str(tenant)},
                )
            ).scalar_one()
        assert run_status == "completed"
        assert [row[2] for row in opportunities.records] == [1, 2]
        assert len(
            [
                key
                for level, key in notifier.attempts
                if level == "boss_reminder" and key.endswith(":3")
            ]
        ) <= 1
        clock.value += t2
        assert await engine.poll_due(tenant, 1) == 0
    finally:
        await db.dispose()
