"""Task 15：受控外部端口下的寻源 V2 真实栈验收。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from apps.scheduler_worker.research_acceptance_dependencies import (
    AcceptancePlaybookReader,
)
from infra.db.outbox import PostgresEventBus
from infra.db.tables import (
    CostSheetRow,
    OutboxEventRow,
    ProductCandidateSourceRow,
    ProductRow,
    SearchQuotaAccountRow,
    SourcingCaseRow,
    SourcingSupplyOptionRow,
    ToolCallRow,
    ValidatedNeedRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from shared.events.catalog import NeedValidated
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    ProspectAccountId,
    ValidatedNeedId,
    new_id,
)
from tests.e2e.conftest import (
    E2EStack,
    _seed_controlled_public_research_policy,
    _seed_controlled_research_playbook,
    e2e_stack_lifecycle,
)

_NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


async def _eventually[T](
    predicate: Callable[[], Awaitable[T | None]],
    *,
    description: str,
    timeout_seconds: float = 30,
) -> T:
    """等待真实 worker / API 因果链，而非以 sleep 猜测时序。"""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        value = await predicate()
        if value is not None:
            return value
        await asyncio.sleep(0.2)
    raise AssertionError(f"Task 15 等待超时：{description}")


async def _create_validated_need(stack: Any) -> str:
    """以真实 PostgreSQL 的 ValidatedNeed 前置状态只发布 NeedValidated。"""

    source_message_id = "msg_task15_need"
    need_id = ValidatedNeedId(new_id("need"))
    account_id = ProspectAccountId(new_id("acc"))

    def fact(value: str | int) -> dict[str, object]:
        return {
            "value": value,
            "provenance": {
                "source_type": "conversation",
                "source_id": source_message_id,
                "extracted_by": "human",
                "extracted_at": _NOW.isoformat(),
                "confirmed_by": None,
                "confirmed_at": None,
                "source_url": None,
                "page_hash": None,
                "source_quote": None,
            },
        }

    async with stack.factory() as session:
        # 受控寻源子项目从已验证 Need 开始。这是真实 PG 数据状态，不构造
        # DemandSignal/Hypothesis，也不越过 domain 的公开 service 边界。
        session.add(
            ValidatedNeedRow(
                tenant_id=str(stack.tenant_id),
                need_id=str(need_id),
                account_id=str(account_id),
                product_category=fact("hinges"),
                source_message_id=source_message_id,
                source_conversation_id=None,
                status="sourcing_ready",
                created_at=_NOW,
                application=fact("marine"),
                material=fact("304 stainless steel"),
                size_spec=fact("4 inch"),
                quantity=fact(500),
            )
        )
        await PostgresEventBus(session, stack.tenant_id, now=lambda: _NOW).publish(
            NeedValidated(
                tenant_id=stack.tenant_id,
                occurred_at=_NOW,
                need_id=need_id,
                account_id=account_id,
                category="hinges",
                evidence_level=EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING,
                completeness=3,
            )
        )
        await session.commit()
    return str(need_id)


async def _seed_confirmed_free_tavily_usage(stack: Any) -> None:
    """为受控 Tavily 端口提供真实持久化的免费额度观察事实。"""

    async with stack.factory() as session:
        session.add(
            SearchQuotaAccountRow(
                tenant_id=str(stack.tenant_id),
                provider="tavily",
                ceiling=10,
                reservations=0,
                cost_status="free",
                usage_limit=10,
                usage_used=0,
                paygo_enabled=False,
                checked_at=_NOW,
            )
        )
        await session.commit()


def _headers(stack: Any) -> dict[str, str]:
    return {
        "X-Tenant-Id": str(stack.tenant_id),
        "X-Employee-Id": str(stack.employees.boss),
    }


@pytest.mark.asyncio(loop_scope="session")
async def _run_controlled_need_to_estimated_cost(
    e2e_stack: Any,
) -> None:
    """验收真实 Need→Case→产品卡→审核→Opportunity→ESTIMATED 成本的窄链。"""

    assert (
        "sourcing_case.v2.check_ladder"
        in e2e_stack.scheduler_runtime.workflow._handlers
    )
    need_id = await _create_validated_need(e2e_stack)
    await _seed_controlled_public_research_policy(
        e2e_stack.factory,
        e2e_stack.tenant_id,
        e2e_stack.employees.boss,
        e2e_stack.employees.manager,
    )
    await _seed_controlled_research_playbook(
        e2e_stack.factory,
        e2e_stack.tenant_id,
        e2e_stack.employees.boss,
        e2e_stack.employees.manager,
    )
    await _seed_confirmed_free_tavily_usage(e2e_stack)
    headers = _headers(e2e_stack)

    async with httpx.AsyncClient(
        base_url=e2e_stack.api_origin, headers=headers, timeout=10
    ) as client:

        async def admission_waiting() -> dict[str, Any] | None:
            response = await client.get("/sourcing-admissions?state=waiting")
            assert response.status_code == 200, response.text
            for item in response.json()["items"]:
                if item["need_id"] == need_id and item["state"] == "waiting":
                    return item
            return None

        await _eventually(
            admission_waiting,
            description="Need 进入真实 Sourcing Admission 等待队列",
        )
        async with e2e_stack.factory() as session:
            run_count_before_policy = int(
                await session.scalar(
                    select(func.count())
                    .select_from(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.tenant_id == str(e2e_stack.tenant_id),
                        WorkflowRunRow.workflow_type == "sourcing_case",
                    )
                )
                or 0
            )
        assert run_count_before_policy == 0

        proposed = await client.post(
            "/commands/sourcing-admission-proposals",
            json={
                "message": "Task 15 受控链启用需求簇寻源准入，每轮一个案例",
                "mode": "cluster_ranked",
                "automatic_admission_enabled": True,
                "batch_limit": 1,
            },
        )
        assert proposed.status_code == 200, proposed.text
        confirmed_policy = await client.post(
            "/commands/sourcing-admission-proposals/"
            f"{proposed.json()['proposal_id']}/confirm",
            headers={**headers, "Idempotency-Key": "task15-admission-policy"},
        )
        assert confirmed_policy.status_code == 200, confirmed_policy.text
        assert confirmed_policy.json()["batch_limit"] == 1

        async def case_ready() -> dict[str, Any] | None:
            response = await client.get("/sourcing-cases")
            assert response.status_code == 200, response.text
            for item in response.json():
                if item["need_id"] == need_id and item["ladder_checked_to"] == 5:
                    return item
            return None

        case = await _eventually(case_ready, description="Need 触发真实 Sourcing Case")
        case_id = case["case_id"]
        assert case["workflow_version"] == 2
        assert case["state"] == "discovering"
        assert case["version"] == 6

        plan_payload = {
            "plan_id": new_id("spl"),
            "case_id": case_id,
            "target_countries": ["XZ"],
            "product_category": "hinges",
            "queries": [
                {"query_text": "marine hinge supplier", "target_country": "XZ"}
            ],
            "max_search_queries": 1,
            "max_pages_read": 1,
            "provider": "tavily",
            "search_depth": "basic",
            "usage_credits_remaining": 10,
            "worst_case_credits": 1,
            "version": 1,
            "expected_case_version": case["version"],
        }
        drafted = await client.post(
            f"/sourcing-cases/{case_id}/public-search-plan", json=plan_payload
        )
        assert drafted.status_code == 200, drafted.text
        plan = drafted.json()
        assert plan["status"] == "pending_confirmation"

        reference = {
            "plan_id": plan["plan_id"],
            "expected_plan_hash": plan["plan_hash"],
        }
        confirmed = await client.post(
            f"/sourcing-cases/{case_id}/public-search-plan/confirm",
            json=reference,
            headers={**headers, "Idempotency-Key": "task15-plan-confirm"},
        )
        assert confirmed.status_code == 200, confirmed.text

        async def public_plan_boundary_ready() -> str | None:
            async with e2e_stack.factory() as session:
                current_step = await session.scalar(
                    select(WorkflowRunRow.current_step).where(
                        WorkflowRunRow.tenant_id == str(e2e_stack.tenant_id),
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.subject_ref == case_id,
                        WorkflowRunRow.status == "running",
                    )
                )
            return current_step if current_step == "await_public_plan" else None

        await _eventually(
            public_plan_boundary_ready,
            description="计划确认后真实 Workflow 进入 await_public_plan",
        )
        started = await client.post(
            f"/sourcing-cases/{case_id}/run",
            json=reference,
            headers={**headers, "Idempotency-Key": "task15-plan-run"},
        )
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "running"

        sourcing_progress: dict[str, object] = {}

        async def candidate_ready() -> dict[str, Any] | None:
            response = await client.get(f"/sourcing-cases/{case_id}/candidates")
            assert response.status_code == 200, response.text
            candidates = response.json()
            if len(candidates) != 1 or candidates[0]["supply_option"] is None:
                async with e2e_stack.factory() as session:
                    workflow_state = await session.execute(
                        select(
                            WorkflowRunRow.run_id,
                            WorkflowRunRow.current_step,
                            WorkflowRunRow.status,
                            WorkflowRunRow.last_error,
                        ).where(
                            WorkflowRunRow.tenant_id == str(e2e_stack.tenant_id),
                            WorkflowRunRow.workflow_type == "sourcing_case",
                            WorkflowRunRow.subject_ref == case_id,
                        )
                    )
                workflow_rows = workflow_state.all()
                failed = [row for row in workflow_rows if row[2] == "failed"]
                sourcing_progress["candidates"] = candidates
                sourcing_progress["workflow"] = workflow_rows
                if failed:
                    raise AssertionError(f"受控寻源 Workflow failed: {failed}")
                return None
            return candidates[0]

        try:
            candidate = await _eventually(
                candidate_ready, description="公开网页证据、核验与 source_only 产品卡"
            )
        except AssertionError as error:
            raise AssertionError(
                f"{error}; controlled sourcing progress={sourcing_progress}"
            ) from error
        assert candidate["verification_status"] == "qualified"
        assert candidate["price_basis"] == "indicative"
        assert candidate["indicative_price_tiers"][0]["amount"] == "2.50"
        option = candidate["supply_option"]
        assert option["source_kind"] == "supplier_candidate"
        assert option["is_qualified"] is True

        async def review_boundary_ready() -> bool | None:
            async with e2e_stack.factory() as session:
                status = await session.scalar(
                    select(WorkflowStepRow.status)
                    .join(
                        WorkflowRunRow,
                        (WorkflowRunRow.tenant_id == WorkflowStepRow.tenant_id)
                        & (WorkflowRunRow.run_id == WorkflowStepRow.run_id),
                    )
                    .where(
                        WorkflowRunRow.tenant_id == str(e2e_stack.tenant_id),
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.subject_ref == case_id,
                        WorkflowRunRow.current_step == "await_review",
                        WorkflowStepRow.step_name == "await_review",
                    )
                )
            return status == "waiting_event" or None

        await _eventually(
            review_boundary_ready,
            description="候选产品卡后真实 Workflow 等待审核事件",
        )
        case_view = await client.get(f"/sourcing-cases/{case_id}")
        assert case_view.status_code == 200, case_view.text
        review_payload = {
            "primary_option_id": option["option_id"],
            "alternate_option_ids": [],
            "reason": "Task 15 固定公开证据完整，批准仅作内部估算。",
            "expected_case_version": case_view.json()["version"],
        }
        review = await client.post(
            f"/sourcing-cases/{case_id}/review",
            json=review_payload,
            headers={**headers, "Idempotency-Key": "task15-review-confirm"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["confirmed_by"] == str(e2e_stack.employees.boss)
        review_id = review.json()["review_id"]

        async def opportunity_required() -> WorkflowRunRow | None:
            async with e2e_stack.factory() as session:
                run = await session.scalar(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.tenant_id == str(e2e_stack.tenant_id),
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.subject_ref == case_id,
                    )
                )
                handoff_step_status = (
                    await session.scalar(
                        select(WorkflowStepRow.status).where(
                            WorkflowStepRow.tenant_id == str(e2e_stack.tenant_id),
                            WorkflowStepRow.run_id == run.run_id,
                            WorkflowStepRow.step_name == "handoff_costing",
                        )
                    )
                    if run is not None
                    else None
                )
            if (
                run is not None
                and run.current_step == "handoff_costing"
                and run.context.get("sourcing_stop_reason") == "opportunity_required"
                and handoff_step_status == "waiting_event"
            ):
                return run
            return None

        stopped_run = await _eventually(
            opportunity_required,
            description="审核后先观察 Opportunity 缺失停止原因",
        )
        assert stopped_run.status == "running"
        stopped_case = await client.get(f"/sourcing-cases/{case_id}")
        assert stopped_case.status_code == 200, stopped_case.text
        assert stopped_case.json()["stop"] == {
            "code": "opportunity_required",
            "stage": "cost_handoff",
            "query_index": None,
            "provider_http_status": None,
            "observed_count": None,
            "configured_limit": None,
        }

        async def owner_state() -> tuple[object, ...]:
            """跨租户拒绝前后精确读取本租户可变业务状态。"""

            async with e2e_stack.factory() as session:
                case_state = (
                    await session.execute(
                        select(
                            SourcingCaseRow.version,
                            SourcingCaseRow.state,
                            SourcingCaseRow.opportunity_id,
                            SourcingCaseRow.stop_code,
                            SourcingCaseRow.stop_detail,
                        ).where(
                            SourcingCaseRow.tenant_id == str(e2e_stack.tenant_id),
                            SourcingCaseRow.case_id == case_id,
                        )
                    )
                ).one()
                run_state = (
                    await session.execute(
                        select(
                            WorkflowRunRow.run_id,
                            WorkflowRunRow.current_step,
                            WorkflowRunRow.status,
                            WorkflowRunRow.context,
                            WorkflowRunRow.retry_count,
                        ).where(
                            WorkflowRunRow.tenant_id == str(e2e_stack.tenant_id),
                            WorkflowRunRow.workflow_type == "sourcing_case",
                            WorkflowRunRow.subject_ref == case_id,
                        )
                    )
                ).one()
                cost_ids = list(
                    await session.scalars(
                        select(CostSheetRow.cost_sheet_id)
                        .where(CostSheetRow.tenant_id == str(e2e_stack.tenant_id))
                        .order_by(CostSheetRow.cost_sheet_id)
                    )
                )
                tool_ids_before = list(
                    await session.scalars(
                        select(ToolCallRow.tool_call_id)
                        .where(ToolCallRow.tenant_id == str(e2e_stack.tenant_id))
                        .order_by(ToolCallRow.tool_call_id)
                    )
                )
            return case_state, run_state, tuple(cost_ids), tuple(tool_ids_before)

        before_cross_tenant_post = await owner_state()
        cross_tenant = await client.post(
            f"/sourcing-cases/{case_id}/review",
            json=review_payload,
            headers={
                **headers,
                "X-Tenant-Id": new_id("tn"),
                "Idempotency-Key": "task15-cross-tenant-review-post",
            },
        )
        assert cross_tenant.status_code == 403
        assert cross_tenant.json()["code"] == "tenant_forbidden"
        assert await owner_state() == before_cross_tenant_post

        opportunity_body = {
            "request": {
                "need_id": need_id,
                "account_id": new_id("acc"),
                "account_name": "Task 15 Marine Buyer",
                "country": "XZ",
                "product_category": "hinges",
                # 这条受控场景的入站客户会话来自已验证的既有联系渠道；它
                # 不触发任何联系人发现/验证或发送工具调用。机会域目前接收
                # 这个上层判定的布尔事实，而不是在此链路内重新调用联系人域。
                "evidence_tier": "customer_specification",
                "has_verified_contact": True,
                "category_allowed": True,
                "minimum_order_value": {"amount": "1000.00", "currency": "USD"},
                "supply_available": True,
                "quantity": 500,
                "spec_summary": "304 stainless steel marine hinge, 4 inch",
                "application": "marine",
                "field_provenance": {
                    name: {
                        "source_type": "conversation",
                        "source_id": "msg_task15_need",
                        "extracted_by": "human",
                        "extracted_at": _NOW.isoformat(),
                        "confirmed_by": None,
                        "confirmed_at": None,
                    }
                    for name in (
                        "account_name",
                        "country",
                        "quantity",
                        "spec_summary",
                        "application",
                    )
                },
                "estimated_order_value": {"amount": "1250.00", "currency": "USD"},
            },
            "evidence": {
                "level": "customer_specification",
                "provenance": {
                    "source_type": "conversation",
                    "source_id": "msg_task15_need",
                    "extracted_by": "human",
                    "extracted_at": _NOW.isoformat(),
                    "confirmed_by": None,
                    "confirmed_at": None,
                },
            },
        }
        created = await client.post("/crm/opportunities", json=opportunity_body)
        assert created.status_code == 201, created.text
        opportunity = created.json()
        assert opportunity["need_id"] == need_id

        generation_before_restart = e2e_stack.scheduler_generation
        runtime_before_restart = e2e_stack.scheduler_runtime
        await e2e_stack.restart_scheduler()
        assert e2e_stack.scheduler_generation == generation_before_restart + 1
        assert e2e_stack.scheduler_runtime is not runtime_before_restart
        assert not e2e_stack.scheduler_task.done()
        assert (
            "sourcing_case.v2.check_ladder"
            in e2e_stack.scheduler_runtime.workflow._handlers
        )

        handoff_retry = await client.post(
            f"/sourcing-cases/{case_id}/review",
            json=review_payload,
            headers={**headers, "Idempotency-Key": "task15-handoff-retry"},
        )
        assert handoff_retry.status_code == 200, handoff_retry.text
        assert handoff_retry.json()["review_id"] == review_id

        async def cost_ready() -> list[dict[str, Any]] | None:
            response = await client.get(
                f"/costing-quotes/opportunities/{opportunity['opportunity_id']}/cost-sheets"
            )
            assert response.status_code == 200, response.text
            sheets = response.json()
            return sheets if len(sheets) == 1 else None

        sheets = await _eventually(cost_ready, description="审核后真实 Costing handoff")
        assert sheets[0]["version_type"] == "estimated"
        assert sheets[0]["quantity"] == 500
        handed_case = await client.get(f"/sourcing-cases/{case_id}")
        assert handed_case.status_code == 200, handed_case.text
        assert handed_case.json()["state"] == "handed_to_costing"
        assert handed_case.json()["stop"] is None

        replayed = await client.post(
            f"/sourcing-cases/{case_id}/review",
            json=review_payload,
            headers={**headers, "Idempotency-Key": "task15-terminal-review-replay"},
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["review_id"] == review_id

    async with e2e_stack.factory() as session:
        product = await session.scalar(
            select(ProductRow).where(
                ProductRow.tenant_id == str(e2e_stack.tenant_id),
                ProductRow.product_id == option["product_id"],
            )
        )
        source = await session.scalar(
            select(ProductCandidateSourceRow).where(
                ProductCandidateSourceRow.tenant_id == str(e2e_stack.tenant_id),
                ProductCandidateSourceRow.product_id == option["product_id"],
            )
        )
        persisted_option = await session.scalar(
            select(SourcingSupplyOptionRow).where(
                SourcingSupplyOptionRow.tenant_id == str(e2e_stack.tenant_id),
                SourcingSupplyOptionRow.option_id == option["option_id"],
            )
        )
        cost = await session.scalar(
            select(CostSheetRow).where(
                CostSheetRow.tenant_id == str(e2e_stack.tenant_id),
                CostSheetRow.opportunity_id == opportunity["opportunity_id"],
            )
        )
        tool_ids = list(
            await session.scalars(
                select(ToolCallRow.tool_id)
                .where(ToolCallRow.tenant_id == str(e2e_stack.tenant_id))
                .order_by(ToolCallRow.tool_id)
            )
        )
        cost_count = await session.scalar(
            select(func.count())
            .select_from(CostSheetRow)
            .where(
                CostSheetRow.tenant_id == str(e2e_stack.tenant_id),
                CostSheetRow.opportunity_id == opportunity["opportunity_id"],
            )
        )
        run_count = await session.scalar(
            select(func.count())
            .select_from(WorkflowRunRow)
            .where(
                WorkflowRunRow.tenant_id == str(e2e_stack.tenant_id),
                WorkflowRunRow.workflow_type == "sourcing_case",
                WorkflowRunRow.subject_ref == case_id,
            )
        )
        dead_outbox_event_types = list(
            await session.scalars(
                select(OutboxEventRow.event_type)
                .where(
                    OutboxEventRow.tenant_id == str(e2e_stack.tenant_id),
                    OutboxEventRow.status == "dead",
                )
                .order_by(OutboxEventRow.event_type)
            )
        )
        outbox_event_statuses = list(
            await session.execute(
                select(OutboxEventRow.event_type, OutboxEventRow.status)
                .where(OutboxEventRow.tenant_id == str(e2e_stack.tenant_id))
                .order_by(OutboxEventRow.event_type)
            )
        )

    assert product is not None and product.candidate_status == "source_only"
    assert source is not None and source.sourcing_case_id == case_id
    assert (
        persisted_option is not None and persisted_option.source == "supplier_candidate"
    )
    assert cost is not None
    assert cost.version_type == "estimated"
    assert cost.source_sourcing_case_id == case_id
    assert cost.source_option_id == option["option_id"]
    assert cost.source_candidate_id == candidate["candidate_id"]
    assert cost_count == 1
    assert run_count == 1
    assert dead_outbox_event_types == []
    assert all(status == "delivered" for _, status in outbox_event_statuses), (
        outbox_event_statuses
    )
    delivered_event_types = {event_type for event_type, _ in outbox_event_statuses}
    # Task 15 从已验证 Need 前置状态开始；不得为了本子项目伪造 demand/prospecting
    # 事件再由寻源 runtime 空消费。Case/Opportunity 两项交接事实仍须可审计地 delivered。
    assert "DemandSignalCaptured" not in delivered_event_types
    assert "NeedHypothesisCreated" not in delivered_event_types
    assert {"NeedValidated", "SourcingCaseOpened", "OpportunityQualified"} <= (
        delivered_event_types
    )
    # 此控制链的 Gateway durable receipt 必须只包含两项公开只读能力；
    # 因而同时证明 contact/email/send/procurement/Quote 全部没有调用。
    assert tool_ids == ["web.read_page", "web.search"]
    assert e2e_stack.controls.tavily_usage_calls == 1
    assert e2e_stack.controls.tavily_search_calls == 1
    assert e2e_stack.controls.page_fetch_calls == 1
    assert e2e_stack.controls.model_calls == 1
    assert e2e_stack.controls.real_network_calls == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_controlled_need_to_estimated_cost_uses_real_core_only() -> None:
    """追加事实的受控验收必须使用独立真实栈，不能污染共享 E2E 基线。"""

    async for stack in e2e_stack_lifecycle():
        assert isinstance(stack, E2EStack)
        audience_calls_before = len(stack.scheduler_audience.calls)
        await _run_controlled_need_to_estimated_cost(stack)
        # 调度器实际走到 Gateway 时必须读取真实、已激活的 Playbook；不能
        # 以恒真替身绕过组织域。Notification audience 是完整 scheduler
        # 组合的必需依赖；本受控寻源链不得额外触发它。
        assert isinstance(stack.playbook_reader, AcceptancePlaybookReader)
        assert len(stack.scheduler_audience.calls) == audience_calls_before
        return
    raise AssertionError("Task 15 真实 E2E 栈未启动")
