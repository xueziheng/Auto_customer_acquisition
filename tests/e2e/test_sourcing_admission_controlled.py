"""NeedCluster 寻源准入的真实 PostgreSQL / API / scheduler 受控验收。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, text

from apps.scheduler_worker.main import _run_cycle, _same_lock_backend
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.outbox import PostgresEventBus
from infra.db.tables import (
    OutboxEventRow,
    SourcingAdmissionRow,
    SourcingCaseRow,
    SourcingPrioritySnapshotRow,
    ToolCallRow,
    ValidatedNeedRow,
    WorkflowRunRow,
)
from shared.events.catalog import NeedClusterMembershipChanged, NeedValidated
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    NeedClusterId,
    ProspectAccountId,
    ValidatedNeedId,
    new_id,
)
from tests.e2e.conftest import (
    E2EStack,
    _shutdown_scheduler,
    e2e_stack_lifecycle,
)

_OBSERVED_AT = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


@dataclass
class _SeedNeedCluster:
    """受控前置事实；写入仍经真实 tenant-bound repository/UoW。"""

    cluster_id: NeedClusterId
    tenant_id: str
    category: str
    member_need_ids: list[ValidatedNeedId]
    keywords: list[str]
    countries: list[str]
    total_potential_quantity: int
    recurring_demand: bool | None
    created_at: datetime
    updated_at: datetime


async def _eventually[T](
    predicate: Callable[[], Awaitable[T | None]],
    *,
    description: str,
    timeout_seconds: float = 30,
    interval_seconds: float = 0.05,
) -> T:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        result = await predicate()
        if result is not None:
            return result
        await asyncio.sleep(interval_seconds)
    raise AssertionError(f"NeedCluster 寻源准入等待超时：{description}")


def _fact(value: str | int, source_id: str, observed_at: datetime) -> dict[str, object]:
    return {
        "value": value,
        "provenance": {
            "source_type": "conversation",
            "source_id": source_id,
            "extracted_by": "human",
            "extracted_at": observed_at.isoformat(),
            "confirmed_by": None,
            "confirmed_at": None,
            "source_url": None,
            "page_hash": None,
            "source_quote": None,
        },
    }


async def _seed_needs_and_memberships(
    stack: E2EStack,
) -> tuple[list[dict[str, object]], list[str], list[str], str]:
    """建立 8-member、3-member 与 unclustered Need，并发布真实 outbox 事实。"""

    definitions = [
        *(("large-cluster-hinges", "marine door", "304 steel", "4 inch", "US") for _ in range(8)),
        *(("small-cluster-fasteners", "solar frame", "316 steel", "6 mm", "DE") for _ in range(3)),
        ("unclustered-valves", "water line", "brass", "12 mm", "BR"),
    ]
    seeded: list[dict[str, object]] = []
    async with stack.factory() as session:
        bus = PostgresEventBus(session, stack.tenant_id, now=lambda: _OBSERVED_AT)
        for index, (category, application, material, size, country) in enumerate(definitions):
            need_id = ValidatedNeedId(new_id("need"))
            account_id = ProspectAccountId(new_id("acc"))
            ready_at = _OBSERVED_AT + timedelta(minutes=index)
            source_id = f"msg-need-cluster-admission-{index}"
            quantity = 1_000 + index
            session.add(
                ValidatedNeedRow(
                    tenant_id=str(stack.tenant_id),
                    need_id=str(need_id),
                    account_id=str(account_id),
                    product_category=_fact(category, source_id, ready_at),
                    source_message_id=source_id,
                    source_conversation_id=None,
                    status="sourcing_ready",
                    created_at=ready_at,
                    application=_fact(application, source_id, ready_at),
                    material=_fact(material, source_id, ready_at),
                    size_spec=_fact(size, source_id, ready_at),
                    quantity=_fact(quantity, source_id, ready_at),
                )
            )
            await bus.publish(
                NeedValidated(
                    tenant_id=stack.tenant_id,
                    occurred_at=ready_at,
                    need_id=need_id,
                    account_id=account_id,
                    category=category,
                    evidence_level=EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING,
                    completeness=3,
                )
            )
            seeded.append(
                {
                    "need_id": str(need_id),
                    "account_id": str(account_id),
                    "category": category,
                    "quantity": quantity,
                    "ready_at": ready_at,
                }
            )
        await session.commit()

    large_ids = [str(item["need_id"]) for item in seeded[:8]]
    small_ids = [str(item["need_id"]) for item in seeded[8:11]]
    clustered_at = _OBSERVED_AT + timedelta(hours=1)
    async with SqlAlchemyDemandUnitOfWork(
        stack.factory,
        stack.tenant_id,
        now=lambda: clustered_at,
    ) as uow:
        for category, items, countries in (
            ("large-cluster-hinges", seeded[:8], ["US"]),
            ("small-cluster-fasteners", seeded[8:11], ["DE"]),
        ):
            cluster_id = NeedClusterId(new_id("ncl"))
            member_ids = [ValidatedNeedId(str(item["need_id"])) for item in items]
            await uow.clusters.add(
                _SeedNeedCluster(
                    cluster_id=cluster_id,
                    tenant_id=stack.tenant_id,
                    category=category,
                    member_need_ids=member_ids,
                    keywords=[category],
                    countries=countries,
                    total_potential_quantity=sum(int(item["quantity"]) for item in items),
                    recurring_demand=None,
                    created_at=clustered_at,
                    updated_at=clustered_at,
                )  # type: ignore[arg-type]
            )
            for need_id in member_ids:
                need = await uow.needs.get(stack.tenant_id, need_id)
                assert need is not None
                await uow.needs.update(replace(need, cluster_id=cluster_id))
                await uow.bus.publish(
                    NeedClusterMembershipChanged(
                        tenant_id=stack.tenant_id,
                        occurred_at=clustered_at,
                        cluster_id=cluster_id,
                        changed_need_id=need_id,
                        member_count=len(member_ids),
                    )
                )
    return seeded, large_ids, small_ids, str(seeded[11]["need_id"])


def _headers(stack: E2EStack) -> dict[str, str]:
    return {
        "X-Tenant-Id": str(stack.tenant_id),
        "X-Employee-Id": str(stack.employees.boss),
    }


async def _run_count(stack: E2EStack) -> int:
    async with stack.factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(WorkflowRunRow)
                .where(
                    WorkflowRunRow.tenant_id == str(stack.tenant_id),
                    WorkflowRunRow.workflow_type == "sourcing_case",
                    WorkflowRunRow.workflow_version == 2,
                )
            )
            or 0
        )


async def _run_one_locked_cycle(stack: E2EStack, cycle: int) -> None:
    """用真实 PostgreSQL session advisory lock 执行一个完整 scheduler cycle。"""

    async with stack.engine.connect() as connection:
        lock_row = (
            await connection.execute(
                text(
                    "SELECT pg_try_advisory_lock(:lock_key) AS acquired, "
                    "pg_backend_pid() AS backend_pid"
                ),
                {"lock_key": stack.scheduler_runtime.config.lock_key},
            )
        ).one()
        await connection.commit()
        assert lock_row.acquired is True

        async def confirm_lock() -> None:
            assert await _same_lock_backend(connection, int(lock_row.backend_pid))

        try:
            await _run_cycle(
                stack.scheduler_runtime,
                cycle,
                confirm_lock=confirm_lock,
            )
        finally:
            released = await connection.scalar(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": stack.scheduler_runtime.config.lock_key},
            )
            await connection.commit()
            assert released is True


async def _assert_separate_need_snapshots(
    stack: E2EStack,
    seeded: list[dict[str, object]],
) -> None:
    expected_quantities = {
        str(item["need_id"]): item["quantity"] for item in seeded
    }
    async with stack.factory() as session:
        cases = list(
            (
                await session.scalars(
                    select(SourcingCaseRow)
                    .where(SourcingCaseRow.tenant_id == str(stack.tenant_id))
                    .order_by(SourcingCaseRow.need_id)
                )
            ).all()
        )
        current_snapshots = list(
            (
                await session.execute(
                    select(
                        SourcingAdmissionRow.case_id.label("admission_case_id"),
                        SourcingAdmissionRow.need_id.label("admission_need_id"),
                        SourcingAdmissionRow.current_snapshot_id,
                        SourcingPrioritySnapshotRow.case_id.label("snapshot_case_id"),
                        SourcingPrioritySnapshotRow.need_id.label("snapshot_need_id"),
                    )
                    .join(
                        SourcingPrioritySnapshotRow,
                        (SourcingPrioritySnapshotRow.tenant_id == SourcingAdmissionRow.tenant_id)
                        & (SourcingPrioritySnapshotRow.snapshot_id == SourcingAdmissionRow.current_snapshot_id),
                    )
                    .where(SourcingAdmissionRow.tenant_id == str(stack.tenant_id))
                )
            ).all()
        )
    assert len(cases) == len(seeded) == 12
    assert len({case.case_id for case in cases}) == 12
    assert len({case.need_id for case in cases}) == 12
    for case in cases:
        assert case.need_snapshot["need_id"] == case.need_id
        assert case.need_snapshot["quantity"]["value"] == expected_quantities[case.need_id]
    assert len(current_snapshots) == 12
    assert len({row.current_snapshot_id for row in current_snapshots}) == 12
    assert all(
        row.admission_case_id == row.snapshot_case_id
        and row.admission_need_id == row.snapshot_need_id
        for row in current_snapshots
    )


async def _publish_duplicate_readiness(
    stack: E2EStack,
    seeded: list[dict[str, object]],
) -> None:
    async with stack.factory() as session:
        bus = PostgresEventBus(session, stack.tenant_id)
        for item in seeded:
            await bus.publish(
                NeedValidated(
                    tenant_id=stack.tenant_id,
                    occurred_at=item["ready_at"],
                    need_id=ValidatedNeedId(str(item["need_id"])),
                    account_id=ProspectAccountId(str(item["account_id"])),
                    category=str(item["category"]),
                    evidence_level=EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING,
                    completeness=3,
                )
            )
        await session.commit()


async def _accept_controlled_sourcing_admission(stack: E2EStack) -> None:
    # 先停止后台循环，完整提交受控事实，再用一个真实锁定 cycle 确定性投递。
    # 这避免 fixture 启动中的 worker 与测试造数并发；生产排序语义不变。
    await _shutdown_scheduler(stack.scheduler_task, stack.scheduler_stop)
    seeded, large_ids, small_ids, unclustered_id = await _seed_needs_and_memberships(stack)
    await _run_one_locked_cycle(stack, 0)
    headers = _headers(stack)
    async with httpx.AsyncClient(
        base_url=stack.api_origin,
        headers=headers,
        timeout=10,
    ) as client:
        async def waiting_snapshot() -> dict[str, object] | None:
            response = await client.get("/sourcing-admissions?state=waiting&limit=50")
            assert response.status_code == 200, response.text
            payload = response.json()
            items = payload["items"]
            counts = [item["cluster_member_count"] for item in items]
            if len(items) == 12 and counts == [8] * 8 + [3] * 3 + [1]:
                return payload
            return None

        before_policy = await _eventually(
            waiting_snapshot,
            description="12 条准入按 8→3→1 排序",
        )
        assert before_policy["policy"]["status"] == "policy_not_configured"
        waiting_items = before_policy["items"]
        assert [item["need_id"] for item in waiting_items[:8]] == large_ids
        assert [item["need_id"] for item in waiting_items[8:11]] == small_ids
        assert waiting_items[11]["need_id"] == unclustered_id
        assert await _run_count(stack) == 0
        assert stack.controls.tavily_usage_calls == 0
        assert stack.controls.tavily_search_calls == 0
        assert stack.controls.page_fetch_calls == 0
        assert stack.controls.model_calls == 0
        await _assert_separate_need_snapshots(stack, seeded)

        proposed = await client.post(
            "/commands/sourcing-admission-proposals",
            json={
                "message": "按需求簇规模排序，每轮最多启动 2 个寻源案例",
                "mode": "cluster_ranked",
                "automatic_admission_enabled": True,
                "batch_limit": 2,
            },
        )
        assert proposed.status_code == 200, proposed.text
        proposal = proposed.json()
        assert proposal["state"] == "pending_confirmation"
        assert proposal["sourcing_admission_batch_limit"] == 2
        assert await _run_count(stack) == 0

        confirmed = await client.post(
            "/commands/sourcing-admission-proposals/"
            f"{proposal['proposal_id']}/confirm",
            headers={**headers, "Idempotency-Key": "task12-confirm-batch-two"},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["automatic_admission_enabled"] is True
        assert confirmed.json()["batch_limit"] == 2
        assert await _run_count(stack) == 0

        await _run_one_locked_cycle(stack, 1)
        assert await _run_count(stack) == 2
        async with stack.factory() as session:
            first_started = list(
                (
                    await session.scalars(
                        select(SourcingCaseRow.need_id)
                        .join(
                            WorkflowRunRow,
                            (WorkflowRunRow.tenant_id == SourcingCaseRow.tenant_id)
                            & (WorkflowRunRow.subject_ref == SourcingCaseRow.case_id),
                        )
                        .where(
                            SourcingCaseRow.tenant_id == str(stack.tenant_id),
                            WorkflowRunRow.workflow_type == "sourcing_case",
                            WorkflowRunRow.workflow_version == 2,
                        )
                        .order_by(WorkflowRunRow.run_id)
                    )
                ).all()
            )
        assert len(first_started) == 2
        assert set(first_started) == set(large_ids[:2])
        assert stack.controls.tavily_usage_calls == 0
        assert stack.controls.tavily_search_calls == 0
        assert stack.controls.page_fetch_calls == 0
        assert stack.controls.model_calls == 0

        await _publish_duplicate_readiness(stack, seeded)
        await stack.restart_scheduler()
        assert stack.scheduler_generation == 2

        async def exactly_four_runs() -> int | None:
            count = await _run_count(stack)
            if count > 4:
                raise AssertionError("重建 runtime 后单轮启动超过确认的 batch_limit")
            return count if count == 4 else None

        await _eventually(
            exactly_four_runs,
            description="重建 runtime 后继续精确启动下一批 2 条",
        )
        await _shutdown_scheduler(stack.scheduler_task, stack.scheduler_stop)
        async with stack.factory() as session:
            case_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SourcingCaseRow)
                    .where(SourcingCaseRow.tenant_id == str(stack.tenant_id))
                )
                or 0
            )
            run_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.tenant_id == str(stack.tenant_id),
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.workflow_version == 2,
                    )
                )
                or 0
            )
            run_subject_count = int(
                await session.scalar(
                    select(func.count(func.distinct(WorkflowRunRow.subject_ref))).where(
                        WorkflowRunRow.tenant_id == str(stack.tenant_id),
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.workflow_version == 2,
                    )
                )
                or 0
            )
        assert (case_count, run_count, run_subject_count) == (12, 4, 4)

        waiting_after_restart = (
            await client.get("/sourcing-admissions?state=waiting&limit=50")
        ).json()
        manual_target = next(
            item
            for item in waiting_after_restart["items"]
            if item["need_id"] == unclustered_id
        )
        immutable_before = {
            key: manual_target[key]
            for key in (
                "snapshot_id",
                "cluster_id",
                "cluster_member_count",
                "ready_at",
                "facts_observed_at",
                "ranking_version",
                "explanation",
            )
        }
        manual_path = f"/sourcing-admissions/{manual_target['admission_id']}/admit"
        manual_headers = {**headers, "Idempotency-Key": "task12-manual-replay"}
        first_manual = await client.post(manual_path, headers=manual_headers)
        second_manual = await client.post(manual_path, headers=manual_headers)
        assert first_manual.status_code == second_manual.status_code == 200
        assert first_manual.json()["state"] == second_manual.json()["state"] == "admitted"
        assert first_manual.json()["admitted_by"] == str(stack.employees.boss)
        assert second_manual.json()["admitted_at"] == first_manual.json()["admitted_at"]
        detail = await client.get(
            f"/sourcing-admissions/{manual_target['admission_id']}"
        )
        assert detail.status_code == 200, detail.text
        detail_admission = detail.json()["admission"]
        assert {
            key: detail_admission[key] for key in immutable_before
        } == immutable_before
        assert detail_admission["state"] == "admitted"

        assert await _run_count(stack) == 5
        async with stack.factory() as session:
            manual_runs = int(
                await session.scalar(
                    select(func.count())
                    .select_from(WorkflowRunRow)
                    .join(
                        SourcingCaseRow,
                        (SourcingCaseRow.tenant_id == WorkflowRunRow.tenant_id)
                        & (SourcingCaseRow.case_id == WorkflowRunRow.subject_ref),
                    )
                    .where(
                        WorkflowRunRow.tenant_id == str(stack.tenant_id),
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.workflow_version == 2,
                        SourcingCaseRow.need_id == unclustered_id,
                    )
                )
                or 0
            )
            tool_calls = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ToolCallRow)
                    .where(ToolCallRow.tenant_id == str(stack.tenant_id))
                )
                or 0
            )
            dead_events = list(
                (
                    await session.scalars(
                        select(OutboxEventRow.event_type).where(
                            OutboxEventRow.tenant_id == str(stack.tenant_id),
                            OutboxEventRow.status == "dead",
                        )
                    )
                ).all()
            )
        assert manual_runs == 1
        assert tool_calls == 0
        assert dead_events == []
        assert stack.controls.tavily_usage_calls == 0
        assert stack.controls.tavily_search_calls == 0
        assert stack.controls.page_validate_calls == 0
        assert stack.controls.page_fetch_calls == 0
        assert stack.controls.model_calls == 0
        assert stack.controls.real_network_calls == 0
        await _assert_separate_need_snapshots(stack, seeded)

        web = await client.get(f"{stack.web_origin}/sourcing")
        assert web.status_code == 200


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_need_cluster_sourcing_admission_real_core_is_bounded_and_recoverable() -> None:
    """真实核心止于 Workflow start；外部研究、联系人、采购与报价调用必须为零。"""

    async for stack in e2e_stack_lifecycle():
        await _accept_controlled_sourcing_admission(stack)
        return
    raise AssertionError("NeedCluster 寻源准入受控栈未启动")
