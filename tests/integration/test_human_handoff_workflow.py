"""S3-10 human_handoff 集成：0007 round-trip/只增与真实 Postgres 状态机。"""

from __future__ import annotations

import importlib
import os
import subprocess
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
from shared.events.catalog import HandoffRequested
from shared.schemas.identifiers import EmployeeId, HandoffId, OpportunityId, TenantId

_ROOT = Path(__file__).resolve().parents[2]
_BASE = datetime(2026, 8, 9, 1, 0, 0, tzinfo=UTC)


def _run_alembic(db_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        ["alembic", *args], cwd=_ROOT, env=env, capture_output=True, check=False
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
    def __init__(self) -> None:
        tenant = TenantId("tFlowIntegration")
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


class _Registry:
    def __init__(self) -> None:
        self.rows = []

    def register_handler(self, event_type, handler_name, handler) -> None:
        self.rows.append((event_type, handler_name, handler))


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
