"""S3-20 真实机会看板演示的端到端集成验证。

子进程只接收 testcontainers/Alembic fixture 提供的 ``DATABASE_URL``；成功后不
信任 stdout 的自述，而是用 summary 中的随机 tenant 在同一真实 PostgreSQL 内重新
读取持久化事实。失败路径另外以含唯一 marker 的无效 DSN 验证进程边界不回显凭证。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import (
    EmployeeRow,
    HandoffEscalationRow,
    HandoffRow,
    NotificationDeliveryRow,
    OpportunityRow,
    OutboxEventRow,
    OwnershipLockRow,
    ProvenanceRecordRow,
    ScoreSnapshotRow,
    WorkflowRunRow,
)
from shared.schemas.evidence import EvidenceItem, EvidenceLevel, derive_confidence
from shared.schemas.identifiers import TenantId
from shared.schemas.provenance import SourceType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_VERBATIM = "演示客户原话不得输出"
_DEMO_EVIDENCE_URL = "https://evidence.invalid/s3-20-demo"
_DEMO_NOW = datetime(2026, 8, 9, tzinfo=UTC)
_SAFE_LOG_KEYS = frozenset(
    {
        "logger",
        "message",
        "tenant_id",
        "recipient",
        "priority",
        "dedup_key",
        "source_event",
    }
)


def _run_demo(database_url: str) -> subprocess.CompletedProcess[str]:
    """以最小环境启动脚本；调用方断言退出行为，避免 subprocess 回显连接串。"""
    return subprocess.run(
        [sys.executable, "scripts/demo_opportunity_board.py"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
        env={"DATABASE_URL": database_url, "PYTHONPATH": str(_REPO_ROOT)},
        check=False,
    )


def _successful_summary(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """只接受成功脚本的 JSON summary；持久化断言仍以真实数据库为准。"""
    assert result.returncode == 0, "演示脚本应以 0 退出"
    summary = json.loads(result.stdout)
    assert isinstance(summary, dict)
    return summary


async def test_demo_opportunity_board_persists_real_handoff_workflow_and_safe_logs(
    db_url: str,
) -> None:
    """真实演示必须留下五条机会、队列、T1/T2 审计与 durable 通知事实。"""
    result = _run_demo(str(db_url))
    summary = _successful_summary(result)
    assert set(summary) == {
        "tenant_id",
        "employee_ids",
        "opportunity_ids",
        "lost",
        "handoff_queue",
        "target_handoff_id",
        "escalation_levels",
    }
    tenant_id = TenantId(summary["tenant_id"])
    opportunity_ids = summary["opportunity_ids"]
    handoff_queue = summary["handoff_queue"]
    target_handoff_id = summary["target_handoff_id"]
    assert isinstance(opportunity_ids, list) and len(opportunity_ids) == 5
    assert isinstance(handoff_queue, list) and len(handoff_queue) == 2
    assert target_handoff_id == handoff_queue[0]
    assert summary["escalation_levels"] == [1, 2]

    engine = create_engine_from(str(db_url))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            opportunities = (
                await session.execute(
                    select(OpportunityRow)
                    .where(OpportunityRow.tenant_id == str(tenant_id))
                    .order_by(OpportunityRow.created_at.asc(), OpportunityRow.opportunity_id.asc())
                )
            ).scalars().all()
            assert len(opportunities) == 5
            assert {row.opportunity_id for row in opportunities} == set(opportunity_ids)
            employees = (
                await session.execute(
                    select(EmployeeRow).where(EmployeeRow.tenant_id == str(tenant_id))
                )
            ).scalars().all()
            employees_by_id = {row.employee_id: row for row in employees}
            sales_ids = {row.employee_id for row in employees if row.role == "sales"}
            boss_ids = {row.employee_id for row in employees if row.role == "boss"}
            assert len(sales_ids) == 2
            assert len(boss_ids) == 1
            assert {row.owner for row in opportunities} == sales_ids

            lost = summary["lost"]
            assert isinstance(lost, dict)
            lost_row = next(
                row for row in opportunities if row.opportunity_id == lost["opportunity_id"]
            )
            assert lost == {
                "opportunity_id": lost_row.opportunity_id,
                "state": "lost",
                "loss_reason": "price_too_high",
                "died_at_state": "assigned",
            }
            assert lost_row.state == "lost"
            assert lost_row.loss_reason == "price_too_high"
            assert lost_row.died_at_state == "assigned"
            assert lost_row.closed_by in boss_ids
            assert lost_row.closed_at is not None

            handoffs = (
                await session.execute(
                    select(HandoffRow)
                    .where(
                        HandoffRow.tenant_id == str(tenant_id),
                        HandoffRow.state == "requested",
                    )
                    .order_by(HandoffRow.requested_at.asc(), HandoffRow.handoff_id.asc())
                )
            ).scalars().all()
            assert [row.handoff_id for row in handoffs] == handoff_queue
            assert handoffs[0].requested_at < handoffs[1].requested_at
            assert all(row.customer_verbatim == _DEMO_VERBATIM for row in handoffs)
            assert all(row.evidence_links == [_DEMO_EVIDENCE_URL] for row in handoffs)
            opportunities_by_id = {row.opportunity_id: row for row in opportunities}
            for handoff in handoffs:
                opportunity = opportunities_by_id[handoff.opportunity_id]
                assert handoff.account_name == opportunity.account_name
                assert handoff.country == opportunity.country

            target_run = (
                await session.execute(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.tenant_id == str(tenant_id),
                        WorkflowRunRow.subject_ref == target_handoff_id,
                    )
                )
            ).scalar_one()
            assert target_run.status == "running"
            assert target_run.current_step == "wait_acceptance_boss"
            target_handoff = handoffs[0]
            target_opportunity = opportunities_by_id[target_handoff.opportunity_id]
            assert target_opportunity.owner == target_handoff.assigned_to
            assert target_run.context["assigned_to"] == target_handoff.assigned_to

            escalation_levels = (
                await session.execute(
                    select(HandoffEscalationRow.level)
                    .where(
                        HandoffEscalationRow.tenant_id == str(tenant_id),
                        HandoffEscalationRow.handoff_id == target_handoff_id,
                    )
                    .order_by(HandoffEscalationRow.level.asc())
                )
            ).scalars().all()
            assert escalation_levels == [1, 2]
            second_escalations = (
                await session.execute(
                    select(HandoffEscalationRow.escalation_id).where(
                        HandoffEscalationRow.tenant_id == str(tenant_id),
                        HandoffEscalationRow.handoff_id == handoff_queue[1],
                    )
                )
            ).scalars().all()
            assert second_escalations == []

            requested_events = (
                await session.execute(
                    select(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant_id),
                        OutboxEventRow.event_type == "HandoffRequested",
                    )
                    .order_by(OutboxEventRow.occurred_at.asc())
                )
            ).scalars().all()
            assert len(requested_events) == 2
            assert all(event.status == "delivered" for event in requested_events)

            notifications = (
                await session.execute(
                    select(NotificationDeliveryRow)
                    .where(
                        NotificationDeliveryRow.tenant_id == str(tenant_id),
                        NotificationDeliveryRow.dedup_key.in_(
                            [
                                f"human_handoff:{target_handoff_id}:owner",
                                f"human_handoff:{target_handoff_id}:manager",
                                f"human_handoff:{target_handoff_id}:boss",
                            ]
                        ),
                    )
                    .order_by(NotificationDeliveryRow.dedup_key.asc())
                )
            ).scalars().all()
            assert {row.dedup_key for row in notifications} == {
                f"human_handoff:{target_handoff_id}:owner",
                f"human_handoff:{target_handoff_id}:manager",
                f"human_handoff:{target_handoff_id}:boss",
            }
            assert all(row.channel_name == "structured_log" for row in notifications)
            assert all(row.status == "delivered" for row in notifications)

        log_records = [json.loads(line) for line in result.stderr.splitlines() if line]
        assert len(log_records) == 3
        for record in log_records:
            assert set(record) == _SAFE_LOG_KEYS
            assert record["logger"] == "notification_gateway.channels.structured_log"
            assert record["message"] == "通知投递（structured_log）"
            assert record["tenant_id"] == str(tenant_id)
            assert record["priority"] == "urgent"
            assert record["source_event"] == "HandoffRequested"
        target_owner = target_handoff.assigned_to
        assert target_owner is not None
        target_manager = employees_by_id[target_owner].manager_id
        assert target_manager is not None
        recipients_by_suffix = {
            record["dedup_key"].rsplit(":", 1)[-1]: record["recipient"]
            for record in log_records
        }
        assert recipients_by_suffix == {
            "owner": target_owner,
            "manager": target_manager,
            "boss": next(iter(boss_ids)),
        }
    finally:
        await engine.dispose()

    output = result.stdout + result.stderr
    assert str(db_url) not in output
    dsn_secret_fragment = urlsplit(str(db_url)).password
    assert dsn_secret_fragment is None or dsn_secret_fragment not in output
    assert _DEMO_VERBATIM not in output
    assert _DEMO_EVIDENCE_URL not in output


async def test_demo_opportunity_board_derives_snapshot_tier_from_customer_evidence(
    db_url: str,
) -> None:
    """改成手填 tier 或绕开确定性推导时，五条真实 snapshot 必须暴露偏差。"""
    summary = _successful_summary(_run_demo(str(db_url)))
    tenant_id = TenantId(summary["tenant_id"])
    opportunity_ids = set(summary["opportunity_ids"])
    expected_tier = derive_confidence(
        [
            EvidenceItem(
                level=EvidenceLevel.CUSTOMER_INTEREST_REPLY,
                source_type=SourceType.CONVERSATION.value,
                source_id="demo-message-1",
                observed_at=_DEMO_NOW,
                summary="演示客户确认需求",
            )
        ],
        now=_DEMO_NOW,
    ).tier.value

    engine = create_engine_from(str(db_url))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            snapshots = (
                await session.execute(
                    select(ScoreSnapshotRow).where(
                        ScoreSnapshotRow.tenant_id == str(tenant_id)
                    )
                )
            ).scalars().all()
            assert len(snapshots) == 5
            assert {row.opportunity_id for row in snapshots} == opportunity_ids
            assert {row.evidence_tier for row in snapshots} == {expected_tier}
    finally:
        await engine.dispose()


async def test_demo_opportunity_board_keeps_public_adapter_durable_evidence(
    db_url: str,
) -> None:
    """直插机会会缺失的 score、Provenance、归属锁和合格事件必须全部可查。"""
    summary = _successful_summary(_run_demo(str(db_url)))
    tenant_id = TenantId(summary["tenant_id"])
    opportunity_ids = set(summary["opportunity_ids"])

    engine = create_engine_from(str(db_url))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            opportunities = (
                await session.execute(
                    select(OpportunityRow).where(
                        OpportunityRow.tenant_id == str(tenant_id),
                        OpportunityRow.opportunity_id.in_(opportunity_ids),
                    )
                )
            ).scalars().all()
            assert len(opportunities) == 5

            snapshots = (
                await session.execute(
                    select(ScoreSnapshotRow).where(
                        ScoreSnapshotRow.tenant_id == str(tenant_id),
                        ScoreSnapshotRow.opportunity_id.in_(opportunity_ids),
                    )
                )
            ).scalars().all()
            assert len(snapshots) == 5
            assert {row.opportunity_id for row in snapshots} == opportunity_ids
            assert all(row.failed_gates == [] for row in snapshots)

            provenance_rows = (
                await session.execute(
                    select(ProvenanceRecordRow).where(
                        ProvenanceRecordRow.tenant_id == str(tenant_id),
                        ProvenanceRecordRow.entity_type == "opportunity",
                        ProvenanceRecordRow.entity_id.in_(opportunity_ids),
                        ProvenanceRecordRow.field_name.in_(("account_name", "country")),
                        ProvenanceRecordRow.source_type == SourceType.CONVERSATION.value,
                    )
                )
            ).scalars().all()
            assert {
                (row.entity_id, row.field_name) for row in provenance_rows
            } == {
                (opportunity_id, field_name)
                for opportunity_id in opportunity_ids
                for field_name in ("account_name", "country")
            }

            locks = (
                await session.execute(
                    select(OwnershipLockRow).where(
                        OwnershipLockRow.tenant_id == str(tenant_id),
                        OwnershipLockRow.account_id.in_(
                            {row.account_id for row in opportunities}
                        ),
                    )
                )
            ).scalars().all()
            locks_by_account = {row.account_id: row for row in locks}
            assert len(locks_by_account) == 5
            for opportunity in opportunities:
                lock = locks_by_account[opportunity.account_id]
                assert lock.owner == opportunity.owner
                assert lock.locked_by_rule == "3"

            qualified_events = (
                await session.execute(
                    select(OutboxEventRow).where(
                        OutboxEventRow.tenant_id == str(tenant_id),
                        OutboxEventRow.event_type == "OpportunityQualified",
                    )
                )
            ).scalars().all()
            assert len(qualified_events) == 5
            assert {
                event.event_payload["opportunity_id"] for event in qualified_events
            } == opportunity_ids
            assert {event.status for event in qualified_events} <= {
                "pending",
                "delivered",
                "dead",
            }
    finally:
        await engine.dispose()


async def test_demo_opportunity_board_runs_twice_in_one_database_with_isolated_tenants(
    db_url: str,
) -> None:
    """固定员工 ID 会让第二次运行失败；每次演示必须拥有独立员工和五条机会。"""
    first_result = _run_demo(str(db_url))
    second_result = _run_demo(str(db_url))
    assert (first_result.returncode, second_result.returncode) == (0, 0)

    first_summary = _successful_summary(first_result)
    second_summary = _successful_summary(second_result)
    first_tenant = TenantId(first_summary["tenant_id"])
    second_tenant = TenantId(second_summary["tenant_id"])
    assert first_tenant != second_tenant

    def employee_ids(summary: dict[str, object]) -> set[str]:
        raw = summary["employee_ids"]
        assert isinstance(raw, dict)
        assert set(raw) == {"operator", "manager", "sales_a", "sales_b"}
        assert all(isinstance(employee_id, str) for employee_id in raw.values())
        return set(raw.values())

    assert employee_ids(first_summary).isdisjoint(employee_ids(second_summary))

    engine = create_engine_from(str(db_url))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for tenant_id, summary in (
                (first_tenant, first_summary),
                (second_tenant, second_summary),
            ):
                opportunities = (
                    await session.execute(
                        select(OpportunityRow).where(
                            OpportunityRow.tenant_id == str(tenant_id)
                        )
                    )
                ).scalars().all()
                assert len(opportunities) == 5
                assert {row.opportunity_id for row in opportunities} == set(
                    summary["opportunity_ids"]
                )
    finally:
        await engine.dispose()


def test_demo_opportunity_board_failure_redacts_invalid_database_url() -> None:
    """连接失败也只能返回固定中文消息，不能把 DSN/marker 或 traceback 写入 stderr。"""
    marker = "s3_20_unique_secret_marker"
    invalid_url = (
        "postgresql+asyncpg://" + "demo" + ":" + marker + "@127.0.0.1:1/unreachable"
    )
    result = _run_demo(invalid_url)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "演示运行失败\n"
    assert invalid_url not in result.stdout + result.stderr
    assert marker not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
