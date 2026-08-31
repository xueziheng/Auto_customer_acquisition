"""Task 15：受控外部端口下的寻源 V2 真实栈验收。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from apps.scheduler_worker.research_acceptance_dependencies import (
    AcceptancePlaybookReader,
)
from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service_impl import DemandServiceImpl
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.tables import (
    CostSheetRow,
    ProductCandidateSourceRow,
    ProductRow,
    SearchQuotaAccountRow,
    SourcingSupplyOptionRow,
    ToolCallRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from shared.schemas.identifiers import ProspectAccountId, new_id
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
    """通过真实 Demand service + Outbox 创建具备寻源完整度的 Need。"""

    service = DemandServiceImpl(
        lambda tenant_id: SqlAlchemyDemandUnitOfWork(
            stack.factory, tenant_id, now=lambda: _NOW
        ),
        now=lambda: _NOW,
    )
    signal_id = await service.capture_signal(
        stack.tenant_id,
        SignalCaptureRequest(
            signal_type="inbound_inquiry",
            entity_name="Task 15 Marine Buyer",
            raw_observation="Customer requested marine hinges.",
            observed_at=_NOW,
            source_type="conversation",
            source_id="msg_task15_need",
            extracted_by="human",
        ),
    )
    hypothesis_id = await service.create_hypothesis(
        stack.tenant_id,
        ProspectAccountId(new_id("acc")),
        "marine hinges",
        [signal_id],
        "客户明确需要海用铰链。",
        "human",
    )
    return str(
        await service.promote_to_validated(
            stack.tenant_id,
            hypothesis_id,
            "msg_task15_need",
            {
                "product_category": "hinges",
                "application": "marine",
                "material": "304 stainless steel",
                "size_spec": "4 inch",
                "quantity": 500,
            },
        )
    )


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
                        (
                            WorkflowRunRow.tenant_id == WorkflowStepRow.tenant_id
                        )
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
        review = await client.post(
            f"/sourcing-cases/{case_id}/review",
            json={
                "primary_option_id": option["option_id"],
                "alternate_option_ids": [],
                "reason": "Task 15 固定公开证据完整，批准仅作内部估算。",
                "expected_case_version": case_view.json()["version"],
            },
            headers={**headers, "Idempotency-Key": "task15-review-confirm"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["confirmed_by"] == str(e2e_stack.employees.boss)

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

    assert product is not None and product.candidate_status == "source_only"
    assert source is not None and source.sourcing_case_id == case_id
    assert persisted_option is not None and persisted_option.source == "supplier_candidate"
    assert cost is not None
    assert cost.version_type == "estimated"
    assert cost.source_sourcing_case_id == case_id
    assert cost.source_option_id == option["option_id"]
    assert cost.source_candidate_id == candidate["candidate_id"]
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
        await _run_controlled_need_to_estimated_cost(stack)
        # 调度器实际走到 Gateway 时必须读取真实、已激活的 Playbook；不能
        # 以恒真替身绕过组织域。sourcing 事件也没有订阅者，若意外调用
        # audience 则测试栈会立即报错，并在这里保留可审计的零调用断言。
        assert isinstance(stack.playbook_reader, AcceptancePlaybookReader)
        assert stack.scheduler_audience.calls == 0
        return
    raise AssertionError("Task 15 真实 E2E 栈未启动")
