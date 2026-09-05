"""Catalog Product Proposal 的真实 PostgreSQL/API/scheduler/browser 受控验收。"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.async_api import Locator, Page, Request, async_playwright, expect
from sqlalchemy import func, select, text, update

from apps.scheduler_worker.main import _run_cycle, _same_lock_backend
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.outbox import PostgresEventBus, deserialize
from infra.db.tables import (
    ApprovalPackageRow,
    CatalogCultivationCaseRow,
    CatalogProductProposalRow,
    CatalogProposalEvaluationRow,
    CatalogProposalPolicyVersionRow,
    ContactPointRow,
    EmployeeRow,
    OutboxDeliveryRow,
    OutboxEventRow,
    OutreachMessageAttemptRow,
    ProductRow,
    ProspectAccountRow,
    ProspectContactRow,
    QuotationRow,
    SourcingSearchExecutionRow,
    SupplierRow,
    ToolCallRow,
    ValidatedNeedRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from shared.events.catalog import (
    AccountCountryFactsChanged,
    ApprovalDecided,
    NeedClusterMembershipChanged,
)
from shared.schemas.identifiers import (
    EmployeeId,
    NeedClusterId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import Provenance, SourceType
from tests.e2e.conftest import (
    E2EStack,
    _shutdown_scheduler,
    e2e_stack_lifecycle,
)

_OBSERVED_AT = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
_CATEGORY = "three-wheelers"
_WARNING = "这是一项候选产品培养建议，不代表已确认供应、正式产品或可报价价格。"
_POLICY_BODY: dict[str, object] = {
    "minimum_distinct_accounts": 3,
    "minimum_recurring_accounts": None,
    "minimum_distinct_countries": None,
    "minimum_quantity_unit_accounts": None,
    "require_unified_unit": False,
}
_OUTPUT = (
    Path(__file__).resolve().parents[2]
    / "output/playwright/t12-catalog-product-proposal"
)


@dataclass(frozen=True)
class _ControlledCatalogFacts:
    cluster_id: NeedClusterId
    account_ids: tuple[ProspectAccountId, ...]
    need_ids: tuple[ValidatedNeedId, ...]
    product_employee_id: EmployeeId


@dataclass
class _SeedNeedCluster:
    """受控前置事实；只为 tenant-bound Demand repository 提供结构。"""

    cluster_id: NeedClusterId
    tenant_id: TenantId
    category: str
    member_need_ids: list[ValidatedNeedId]
    keywords: list[str]
    countries: list[str]
    total_potential_quantity: int | None
    recurring_demand: bool | None
    created_at: datetime
    updated_at: datetime


def _provenance(source_id: str, confirmer: EmployeeId) -> Provenance:
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=source_id,
        extracted_by="human",
        extracted_at=_OBSERVED_AT,
        confirmed_by=confirmer,
        confirmed_at=_OBSERVED_AT,
    )


def _provenance_json(provenance: Provenance) -> dict[str, object]:
    return {
        "source_type": provenance.source_type.value,
        "source_id": provenance.source_id,
        "extracted_by": provenance.extracted_by,
        "extracted_at": provenance.extracted_at.isoformat(),
        "confirmed_by": (
            None if provenance.confirmed_by is None else str(provenance.confirmed_by)
        ),
        "confirmed_at": (
            None
            if provenance.confirmed_at is None
            else provenance.confirmed_at.isoformat()
        ),
        "source_url": None,
        "page_hash": None,
        "source_quote": None,
    }


def _fact_json(value: str, source_id: str, confirmer: EmployeeId) -> dict[str, object]:
    return {
        "value": value,
        "provenance": _provenance_json(_provenance(source_id, confirmer)),
    }


def _headers(
    stack: E2EStack,
    employee_id: EmployeeId,
    *,
    tenant_id: TenantId | None = None,
) -> dict[str, str]:
    return {
        "X-Tenant-Id": str(tenant_id or stack.tenant_id),
        "X-Employee-Id": str(employee_id),
    }


async def _seed_controlled_catalog_facts(stack: E2EStack) -> _ControlledCatalogFacts:
    """只 seed 合成 Account/Need/Provenance；目录业务行仍全部走真实端口。"""

    product_employee_id = EmployeeId(new_id("emp"))
    account_ids = tuple(ProspectAccountId(new_id("acc")) for _ in range(3))
    need_ids = tuple(ValidatedNeedId(new_id("need")) for _ in range(3))
    async with stack.factory() as session:
        session.add(
            EmployeeRow(
                employee_id=str(product_employee_id),
                tenant_id=str(stack.tenant_id),
                name="Controlled catalog product owner",
                role="product",
            )
        )
        for index, account_id in enumerate(account_ids, start=1):
            name_provenance = _provenance(new_id("msg"), stack.employees.boss)
            country_provenance = _provenance(new_id("msg"), stack.employees.boss)
            session.add(
                ProspectAccountRow(
                    account_id=str(account_id),
                    tenant_id=str(stack.tenant_id),
                    name=f"Controlled Kenya Three-Wheeler Account {index}",
                    country="KE",
                    created_at=_OBSERVED_AT + timedelta(minutes=index),
                    website_domain=None,
                    entity_type=None,
                    industry=None,
                    size_hint=None,
                    source_signal_refs=[],
                    field_provenance={
                        "name": _provenance_json(name_provenance),
                        "country": _provenance_json(country_provenance),
                    },
                )
            )
        for index, (need_id, account_id) in enumerate(
            zip(need_ids, account_ids, strict=True), start=1
        ):
            category_source_id = new_id("msg")
            session.add(
                ValidatedNeedRow(
                    need_id=str(need_id),
                    tenant_id=str(stack.tenant_id),
                    account_id=str(account_id),
                    product_category=_fact_json(
                        _CATEGORY,
                        category_source_id,
                        stack.employees.boss,
                    ),
                    source_message_id=category_source_id,
                    source_conversation_id=None,
                    created_at=_OBSERVED_AT + timedelta(minutes=10 + index),
                    status="validated",
                    confirmed_by=str(stack.employees.boss),
                )
            )
        await session.commit()

    cluster_id = NeedClusterId(new_id("ncl"))
    clustered_at = _OBSERVED_AT + timedelta(hours=1)
    async with SqlAlchemyDemandUnitOfWork(
        stack.factory, stack.tenant_id, now=lambda: clustered_at
    ) as uow:
        await uow.clusters.add(
            _SeedNeedCluster(
                cluster_id=cluster_id,
                tenant_id=stack.tenant_id,
                category=_CATEGORY,
                member_need_ids=list(need_ids),
                keywords=[_CATEGORY],
                countries=["KE"],
                total_potential_quantity=None,
                recurring_demand=None,
                created_at=clustered_at,
                updated_at=clustered_at,
            )  # type: ignore[arg-type]
        )
        for need_id in need_ids:
            need = await uow.needs.get(stack.tenant_id, need_id)
            assert need is not None
            await uow.needs.update(replace(need, cluster_id=cluster_id))
            event = NeedClusterMembershipChanged(
                tenant_id=stack.tenant_id,
                occurred_at=clustered_at,
                cluster_id=cluster_id,
                changed_need_id=need_id,
                member_count=3,
            )
            await uow.bus.publish(event)
            await uow.bus.publish(event)
    return _ControlledCatalogFacts(
        cluster_id=cluster_id,
        account_ids=account_ids,
        need_ids=need_ids,
        product_employee_id=product_employee_id,
    )


async def _run_one_locked_cycle(stack: E2EStack, cycle: int) -> None:
    """用真实 advisory-lock backend 执行一个完整 scheduler cycle。"""

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


async def _catalog_counts(stack: E2EStack) -> dict[str, int]:
    async with stack.factory() as session:
        values = {}
        for name, row in (
            ("policies", CatalogProposalPolicyVersionRow),
            ("evaluations", CatalogProposalEvaluationRow),
            ("proposals", CatalogProductProposalRow),
            ("cultivation_cases", CatalogCultivationCaseRow),
        ):
            values[name] = int(
                await session.scalar(
                    select(func.count())
                    .select_from(row)
                    .where(row.tenant_id == str(stack.tenant_id))
                )
                or 0
            )
        for approval_type, name in (
            ("catalog_proposal_policy_change", "policy_approvals"),
            ("catalog_product_cultivation", "cultivation_approvals"),
        ):
            values[name] = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApprovalPackageRow)
                    .where(
                        ApprovalPackageRow.tenant_id == str(stack.tenant_id),
                        ApprovalPackageRow.approval_type == approval_type,
                    )
                )
                or 0
            )
        return values


async def _side_effect_counts(stack: E2EStack) -> dict[str, int]:
    async with stack.factory() as session:
        values = {}
        for name, row in (
            ("formal_products", ProductRow),
            ("suppliers", SupplierRow),
            ("contacts", ProspectContactRow),
            ("contact_points", ContactPointRow),
            ("search_executions", SourcingSearchExecutionRow),
            ("send_attempts", OutreachMessageAttemptRow),
            ("quotes", QuotationRow),
            ("tool_gateway_calls", ToolCallRow),
        ):
            values[name] = int(
                await session.scalar(
                    select(func.count())
                    .select_from(row)
                    .where(row.tenant_id == str(stack.tenant_id))
                )
                or 0
            )
        return values


async def _drive_until[T](
    stack: E2EStack,
    predicate: Callable[[], Awaitable[T | None]],
    *,
    description: str,
    first_cycle: int,
    maximum_cycles: int = 16,
) -> tuple[T, int]:
    """以有界真实 cycle 等待，并在超时时附安全计数诊断。"""

    for offset in range(maximum_cycles):
        await _run_one_locked_cycle(stack, first_cycle + offset)
        result = await predicate()
        if result is not None:
            return result, first_cycle + offset + 1
    async with stack.factory() as session:
        proposals = list(
            (
                await session.execute(
                    select(
                        CatalogProductProposalRow.proposal_id,
                        CatalogProductProposalRow.state,
                        CatalogProductProposalRow.approval_id,
                    ).where(
                        CatalogProductProposalRow.tenant_id
                        == str(stack.tenant_id)
                    )
                )
            ).all()
        )
        runs = list(
            (
                await session.execute(
                    select(
                        WorkflowRunRow.workflow_type,
                        WorkflowRunRow.subject_ref,
                        WorkflowRunRow.current_step,
                        WorkflowRunRow.status,
                        WorkflowRunRow.retry_count,
                        WorkflowRunRow.last_error,
                    ).where(
                        WorkflowRunRow.tenant_id == str(stack.tenant_id),
                        WorkflowRunRow.workflow_type.like("catalog_%"),
                    )
                )
            ).all()
        )
        steps = list(
            (
                await session.execute(
                    select(
                        WorkflowStepRow.step_name,
                        WorkflowStepRow.status,
                        WorkflowStepRow.attempt,
                        WorkflowStepRow.error,
                    )
                    .join(
                        WorkflowRunRow,
                        (
                            WorkflowRunRow.tenant_id == WorkflowStepRow.tenant_id
                        )
                        & (WorkflowRunRow.run_id == WorkflowStepRow.run_id),
                    )
                    .where(
                        WorkflowRunRow.tenant_id == str(stack.tenant_id),
                        WorkflowRunRow.workflow_type.like("catalog_%"),
                    )
                )
            ).all()
        )
        outbox = list(
            (
                await session.execute(
                    select(
                        OutboxEventRow.event_type,
                        OutboxEventRow.status,
                        OutboxEventRow.attempt,
                        OutboxDeliveryRow.handler_name,
                        OutboxDeliveryRow.status,
                        OutboxDeliveryRow.attempts,
                        OutboxDeliveryRow.last_error,
                    )
                    .outerjoin(
                        OutboxDeliveryRow,
                        (
                            OutboxDeliveryRow.tenant_id
                            == OutboxEventRow.tenant_id
                        )
                        & (OutboxDeliveryRow.event_id == OutboxEventRow.event_id),
                    )
                    .where(
                        OutboxEventRow.tenant_id == str(stack.tenant_id),
                        OutboxEventRow.event_type.in_(
                            (
                                "CatalogProductProposalCreated",
                                "CatalogProposalEvaluated",
                            )
                        ),
                    )
                )
            ).all()
        )
    raise AssertionError(
        f"Catalog 受控验收等待超时：{description}; "
        f"counts={await _catalog_counts(stack)}; proposals={proposals}; "
        f"runs={runs}; steps={steps}; outbox={outbox}"
    )


async def _pending_approval(
    client: httpx.AsyncClient,
    approval_type: str,
) -> dict[str, object] | None:
    response = await client.get("/approvals/pending?limit=100")
    assert response.status_code == 200, response.text
    matches = [
        item for item in response.json() if item["approval_type"] == approval_type
    ]
    if len(matches) > 1:
        raise AssertionError(f"同类待审批不唯一：{approval_type}")
    return matches[0] if matches else None


async def _proposal_with_state(
    client: httpx.AsyncClient,
    state: str,
    *,
    excluded_id: str | None = None,
) -> dict[str, object] | None:
    response = await client.get("/products/catalog-proposals?limit=50")
    assert response.status_code == 200, response.text
    matches = [
        item
        for item in response.json()
        if item["proposal"]["state"] == state
        and item["proposal"]["proposal_id"] != excluded_id
    ]
    if len(matches) > 1:
        raise AssertionError(f"目录提案状态不唯一：{state}")
    return matches[0] if matches else None


async def _republish_approval_decision(
    stack: E2EStack, approval_id: str
) -> None:
    async with stack.factory() as session:
        payloads = list(
            (
                await session.scalars(
                    select(OutboxEventRow.event_payload).where(
                        OutboxEventRow.tenant_id == str(stack.tenant_id),
                        OutboxEventRow.event_type == "ApprovalDecided",
                    )
                )
            ).all()
        )
        matches = [
            payload for payload in payloads if payload.get("approval_id") == approval_id
        ]
        assert matches
        assert all(payload == matches[0] for payload in matches)
        await PostgresEventBus(session, stack.tenant_id).publish(
            deserialize(ApprovalDecided, matches[0])
        )
        await session.commit()


async def _change_country_fact(
    stack: E2EStack,
    account_id: ProspectAccountId,
    country_code: str,
    *,
    publish_event: bool = True,
) -> None:
    """测试专用 fixture 变更；仓库尚无公开 country-correction command。"""

    async with stack.factory() as session:
        row = (
            await session.scalars(
                select(ProspectAccountRow)
                .where(
                    ProspectAccountRow.tenant_id == str(stack.tenant_id),
                    ProspectAccountRow.account_id == str(account_id),
                )
                .with_for_update()
            )
        ).one()
        country_provenance = _provenance(
            new_id("msg"), stack.employees.boss
        )
        provenance_json = {
            "source_type": country_provenance.source_type.value,
            "source_id": country_provenance.source_id,
            "extracted_by": country_provenance.extracted_by,
            "extracted_at": country_provenance.extracted_at.isoformat(),
            "confirmed_by": str(country_provenance.confirmed_by),
            "confirmed_at": country_provenance.confirmed_at.isoformat(),
            "source_url": None,
            "page_hash": None,
            "source_quote": None,
        }
        field_provenance = dict(row.field_provenance)
        field_provenance["country"] = provenance_json
        result = await session.execute(
            update(ProspectAccountRow)
            .where(
                ProspectAccountRow.tenant_id == str(stack.tenant_id),
                ProspectAccountRow.account_id == str(account_id),
            )
            .values(country=country_code, field_provenance=field_provenance)
        )
        assert result.rowcount == 1
        if publish_event:
            await PostgresEventBus(session, stack.tenant_id).publish(
                AccountCountryFactsChanged(
                    tenant_id=stack.tenant_id,
                    occurred_at=_OBSERVED_AT + timedelta(hours=2),
                    account_id=account_id,
                )
            )
        await session.commit()


async def _approve_from_browser(
    page: Page,
    *,
    web_origin: str,
    approval_id: str,
    screenshot_path: Path | None = None,
) -> None:
    await page.goto(f"{web_origin}/approvals?approval_id={approval_id}")
    await expect(page).to_have_url(
        f"{web_origin}/approvals?approval_id={approval_id}"
    )
    await expect(page.get_by_text(approval_id, exact=True)).to_be_visible()
    button = page.get_by_role("button", name="批准此精确变更", exact=True)
    await expect(button).to_be_visible()
    if screenshot_path is not None:
        await page.screenshot(path=screenshot_path, full_page=True)
    async with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == f"/approvals/{approval_id}/decide"
            and response.request.method == "POST"
        )
    ) as pending:
        await button.click()
    response = await pending.value
    assert response.status == 200, await response.text()


def _all_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key) for key in value} | {
            nested for item in value.values() for nested in _all_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _all_keys(item)}
    return set()


async def _assert_page_safe(page: Page) -> None:
    assert await page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )
    assert (
        await page.locator(
            "vite-error-overlay, #vite-error-overlay, .vite-error-overlay"
        ).count()
        == 0
    )


async def _assert_locator_not_clipped(locator: Locator) -> None:
    css_subpixel_tolerance = 0.5
    metrics = await locator.evaluate(
        """
        (element) => {
          const rect = element.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            viewportWidth: window.innerWidth,
            scrollWidth: element.scrollWidth,
            clientWidth: element.clientWidth,
          };
        }
        """
    )
    assert metrics["left"] >= -css_subpixel_tolerance
    assert metrics["right"] <= metrics["viewportWidth"] + css_subpixel_tolerance
    assert metrics["scrollWidth"] <= metrics["clientWidth"]


async def _run_controlled_acceptance(stack: E2EStack, temporary: Path) -> None:
    await _shutdown_scheduler(stack.scheduler_task, stack.scheduler_stop)
    facts = await _seed_controlled_catalog_facts(stack)
    side_effects_before = await _side_effect_counts(stack)
    assert side_effects_before == {
        "formal_products": 0,
        "suppliers": 0,
        "contacts": 0,
        "contact_points": 0,
        "search_executions": 0,
        "send_attempts": 0,
        "quotes": 0,
        "tool_gateway_calls": 0,
    }

    await _run_one_locked_cycle(stack, 0)
    await _run_one_locked_cycle(stack, 1)
    assert await _catalog_counts(stack) == {
        "policies": 0,
        "evaluations": 0,
        "proposals": 0,
        "cultivation_cases": 0,
        "policy_approvals": 0,
        "cultivation_approvals": 0,
    }

    product_headers = _headers(stack, facts.product_employee_id)
    boss_headers = _headers(stack, stack.employees.boss)
    sales_headers = _headers(stack, stack.employees.sales_a)
    approval_request_bodies: list[dict[str, object]] = []
    console_issues: list[str] = []
    page_errors: list[str] = []
    http_issues: list[str] = []

    async with (
        httpx.AsyncClient(
            base_url=stack.api_origin, headers=product_headers, timeout=10
        ) as product_client,
        httpx.AsyncClient(
            base_url=stack.api_origin, headers=boss_headers, timeout=10
        ) as boss_client,
        async_playwright() as playwright,
    ):
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        def capture_console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                console_issues.append(f"{message.type}: {message.text}")

        def capture_page_error(error: Exception) -> None:
            page_errors.append(type(error).__name__)

        def capture_response(response: Any) -> None:
            if response.status >= 400:
                http_issues.append(f"{response.status} {urlsplit(response.url).path}")

        def capture_request(request: Request) -> None:
            path = urlsplit(request.url).path
            if request.method == "POST" and path.endswith("/decide"):
                payload = request.post_data_json
                assert isinstance(payload, dict)
                approval_request_bodies.append(payload)

        page.on("console", capture_console)
        page.on("pageerror", capture_page_error)
        page.on("response", capture_response)
        page.on("request", capture_request)
        try:
            await page.goto(f"{stack.web_origin}/products")
            await expect(page.get_by_text("未配置即关闭", exact=True)).to_be_visible()
            await expect(
                page.get_by_text(
                    "没有活动策略时不会产生新的目录候选产品提案。",
                    exact=True,
                )
            ).to_be_visible()
            await _assert_page_safe(page)
            await page.screenshot(
                path=temporary / "01-no-policy-desktop.png", full_page=True
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            await _assert_page_safe(page)
            await page.screenshot(
                path=temporary / "02-no-policy-390.png", full_page=True
            )
            await page.set_viewport_size({"width": 1280, "height": 900})

            policy_response = await product_client.post(
                "/products/catalog-policies",
                json=_POLICY_BODY,
                headers={"Idempotency-Key": "task15-controlled-catalog-policy-v1"},
            )
            assert policy_response.status_code == 202, policy_response.text
            policy = policy_response.json()["policy"]
            assert policy["content"] == _POLICY_BODY
            assert policy["state"] == "pending_approval"
            assert policy["proposed_by"] == str(facts.product_employee_id)

            async def policy_approval_ready() -> dict[str, object] | None:
                return await _pending_approval(
                    boss_client, "catalog_proposal_policy_change"
                )

            policy_approval, cycle = await _drive_until(
                stack,
                policy_approval_ready,
                description="策略 Approval 创建",
                first_cycle=2,
            )
            policy_approval_id = str(policy_approval["approval_id"])
            await page.goto(f"{stack.web_origin}/products")
            history_row = page.locator(".history-row").filter(
                has_text=policy["policy_version_id"]
            )
            await expect(history_row).to_be_visible()
            policy_link = history_row.get_by_role(
                "link", name="审批状态：待处理", exact=True
            )
            await expect(policy_link).to_have_attribute(
                "href", f"/approvals?approval_id={policy_approval_id}"
            )
            await page.screenshot(
                path=temporary / "03-policy-pending-desktop.png", full_page=True
            )
            await _approve_from_browser(
                page,
                web_origin=stack.web_origin,
                approval_id=policy_approval_id,
                screenshot_path=temporary / "04-policy-approval-deep-link.png",
            )

            async def active_and_pending() -> dict[str, object] | None:
                active = await boss_client.get("/products/catalog-policies/active")
                assert active.status_code == 200, active.text
                proposal = await _proposal_with_state(boss_client, "pending_review")
                if (
                    active.json()
                    and active.json()["policy"]["policy_version_id"]
                    == policy["policy_version_id"]
                    and proposal is not None
                ):
                    return proposal
                return None

            first_proposal, cycle = await _drive_until(
                stack,
                active_and_pending,
                description="精确策略激活并生成首个待审提案",
                first_cycle=cycle,
            )
            first_proposal_id = str(first_proposal["proposal"]["proposal_id"])
            first_approval_id = str(first_proposal["approval"]["approval_id"])

            evaluations_response = await boss_client.get(
                "/products/catalog-evaluations?limit=50"
            )
            assert evaluations_response.status_code == 200
            evaluations = evaluations_response.json()
            assert len(evaluations) == 1
            evaluation = evaluations[0]
            assert evaluation["overall_passed"] is True
            assert evaluation["facts"]["cluster_id"] == str(facts.cluster_id)
            assert evaluation["facts"]["distinct_account_count"] == 3
            assert evaluation["facts"]["known_country_codes"] == ["KE"]
            assert evaluation["facts"]["unknown_country_account_count"] == 0
            assert evaluation["facts"]["recurring_unknown_account_count"] == 3
            assert evaluation["facts"]["quantity_unit_covered_account_count"] == 0
            assert evaluation["facts"]["safe_total_quantity"] is None
            assert evaluation["facts"]["unified_unit"] is None
            assert set(evaluation["facts"]["distinct_account_ids"]) == {
                str(account_id) for account_id in facts.account_ids
            }
            assert set(evaluation["facts"]["member_need_ids"]) == {
                str(need_id) for need_id in facts.need_ids
            }
            rule_states = {
                item["rule"]: item["status"]
                for item in evaluation["rule_results"]
            }
            assert rule_states == {
                "membership_integrity": "passed",
                "distinct_accounts": "passed",
                "recurring_accounts": "unknown",
                "distinct_countries": "not_required",
                "quantity_unit_coverage": "unknown",
                "unified_unit": "unknown",
            }

            await page.goto(f"{stack.web_origin}/products")
            await expect(
                page.get_by_text(
                    f"活动策略 {policy['policy_version_id']}", exact=True
                )
            ).to_be_visible()
            await expect(page.get_by_text("去重客户数至少 3", exact=True)).to_be_visible()
            rules = page.locator("[data-rule-result]")
            await expect(rules).to_have_count(6)
            await expect(rules.filter(has_text="去重客户数")).to_contain_text("通过")
            await expect(rules.filter(has_text="复购客户数")).to_contain_text("未知")
            await expect(rules.filter(has_text="已知国家数")).to_contain_text("不要求")
            await expect(rules.filter(has_text="数量/单位覆盖")).to_contain_text(
                "未知"
            )
            await expect(rules.filter(has_text="统一单位")).to_contain_text("未知")
            await expect(page.get_by_text(_WARNING, exact=True)).to_have_count(2)
            proposal_row = page.locator(".proposal-card").filter(
                has_text=first_proposal_id
            )
            await expect(proposal_row).to_be_visible()
            proposal_link = proposal_row.get_by_role(
                "link", name="审批状态：待处理", exact=True
            )
            await expect(proposal_link).to_have_attribute(
                "href", f"/approvals?approval_id={first_approval_id}"
            )
            body = await page.locator("body").inner_text()
            assert "概率" not in body
            action_text = "\n".join(
                await page.locator("button, a").all_inner_texts()
            )
            for forbidden_action in (
                "自动批准",
                "创建正式 Product",
                "联系供应商",
                "生成客户报价",
            ):
                assert forbidden_action not in action_text
            await _assert_page_safe(page)
            await proposal_row.scroll_into_view_if_needed()
            await page.screenshot(
                path=temporary / "05-active-policy-proposal-desktop.png",
                full_page=True,
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            await proposal_row.scroll_into_view_if_needed()
            await _assert_page_safe(page)
            await _assert_locator_not_clipped(proposal_row)
            await _assert_locator_not_clipped(proposal_row.locator(".status"))
            await _assert_locator_not_clipped(
                proposal_row.locator("dl > div").filter(has_text="负责人").locator("dd")
            )
            await page.screenshot(
                path=temporary / "06-active-policy-proposal-390.png",
                full_page=True,
            )
            await page.set_viewport_size({"width": 1280, "height": 900})
            await _approve_from_browser(
                page,
                web_origin=stack.web_origin,
                approval_id=first_approval_id,
            )

            async def cultivation_ready() -> dict[str, object] | None:
                response = await boss_client.get(
                    "/products/catalog-cultivation-cases?limit=50"
                )
                assert response.status_code == 200, response.text
                items = response.json()
                return items[0] if len(items) == 1 else None

            cultivation, cycle = await _drive_until(
                stack,
                cultivation_ready,
                description="首个提案进入唯一培养队列",
                first_cycle=cycle,
            )
            assert cultivation["cultivation_case"]["state"] == "queued"
            assert cultivation["cultivation_case"]["proposal_id"] == first_proposal_id
            assert cultivation["cultivation_case"]["cluster_id"] == str(
                facts.cluster_id
            )
            assert await _catalog_counts(stack) == {
                "policies": 1,
                "evaluations": 1,
                "proposals": 1,
                "cultivation_cases": 1,
                "policy_approvals": 1,
                "cultivation_approvals": 1,
            }

            await _republish_approval_decision(stack, first_approval_id)
            await _run_one_locked_cycle(stack, cycle)
            cycle += 1
            generation = stack.scheduler_generation
            await stack.restart_scheduler()
            assert stack.scheduler_generation == generation + 1
            await _shutdown_scheduler(stack.scheduler_task, stack.scheduler_stop)
            await _republish_approval_decision(stack, first_approval_id)
            await _run_one_locked_cycle(stack, cycle)
            cycle += 1
            await _run_one_locked_cycle(stack, cycle)
            cycle += 1
            assert await _catalog_counts(stack) == {
                "policies": 1,
                "evaluations": 1,
                "proposals": 1,
                "cultivation_cases": 1,
                "policy_approvals": 1,
                "cultivation_approvals": 1,
            }

            await page.goto(f"{stack.web_origin}/products")
            cultivation_card = page.locator(".cultivation-card").filter(
                has_text=first_proposal_id
            )
            await expect(cultivation_card).to_be_visible()
            await expect(cultivation_card).to_contain_text("去重客户数：3")
            await expect(cultivation_card).to_contain_text("数量/单位证据覆盖：0 / 3")
            await expect(cultivation_card).to_contain_text("复购事实 3 个账户未知")
            await expect(cultivation_card).to_contain_text("安全汇总数量未知")
            await expect(cultivation_card).to_contain_text("统一单位未知")
            await cultivation_card.scroll_into_view_if_needed()
            await page.screenshot(
                path=temporary / "07-queued-cultivation-desktop.png",
                full_page=True,
            )

            await _change_country_fact(
                stack, facts.account_ids[0], "UG"
            )

            async def second_pending() -> dict[str, object] | None:
                return await _proposal_with_state(
                    boss_client,
                    "pending_review",
                    excluded_id=first_proposal_id,
                )

            second_proposal, cycle = await _drive_until(
                stack,
                second_pending,
                description="变更事实生成第二个待审快照",
                first_cycle=cycle,
            )
            second_proposal_id = str(second_proposal["proposal"]["proposal_id"])
            second_approval_id = str(second_proposal["approval"]["approval_id"])
            # 不再触发新评估，专门验证决策时会重读已变的 canonical 事实。
            await _change_country_fact(
                stack,
                facts.account_ids[0],
                "KE",
                publish_event=False,
            )
            stale_decision = await boss_client.post(
                f"/approvals/{second_approval_id}/decide",
                json={"decision": "approve"},
            )
            assert stale_decision.status_code == 200, stale_decision.text

            async def stale_ready() -> dict[str, object] | None:
                return await _proposal_with_state(
                    boss_client, "stale", excluded_id=first_proposal_id
                )

            stale, cycle = await _drive_until(
                stack,
                stale_ready,
                description="旧快照批准被陈旧保护拒绝",
                first_cycle=cycle,
            )
            assert stale["proposal"]["proposal_id"] == second_proposal_id
            restored_snapshot = await _proposal_with_state(
                boss_client,
                "awaiting_approval_submission",
                excluded_id=first_proposal_id,
            )
            assert restored_snapshot is not None
            assert await _catalog_counts(stack) == {
                "policies": 1,
                "evaluations": 3,
                "proposals": 3,
                "cultivation_cases": 1,
                "policy_approvals": 1,
                "cultivation_approvals": 2,
            }

            random_proposal_id = new_id("cpr")
            other_tenant = TenantId(new_id("tn"))
            wrong_tenant_headers = _headers(
                stack, facts.product_employee_id, tenant_id=other_tenant
            )
            cross_existing = await product_client.get(
                f"/products/catalog-proposals/{first_proposal_id}",
                headers=wrong_tenant_headers,
            )
            cross_missing = await product_client.get(
                f"/products/catalog-proposals/{random_proposal_id}",
                headers=wrong_tenant_headers,
            )
            assert cross_existing.status_code == cross_missing.status_code == 403
            assert cross_existing.json() == cross_missing.json()
            denied_read = await product_client.get(
                f"/products/catalog-proposals/{first_proposal_id}",
                headers=sales_headers,
            )
            denied_submit = await product_client.post(
                "/products/catalog-policies",
                headers={
                    **sales_headers,
                    "Idempotency-Key": "task15-denied-sales-policy",
                },
                json=_POLICY_BODY,
            )
            assert denied_read.status_code == denied_submit.status_code == 403

            await page.goto(f"{stack.web_origin}/products")
            await page.locator('[data-proposal-filter="stale"]').click()
            stale_card = page.locator(".proposal-card").filter(
                has_text=second_proposal_id
            )
            await expect(stale_card).to_be_visible()
            await expect(stale_card).to_contain_text("stale")
            await page.set_viewport_size({"width": 390, "height": 844})
            await _assert_page_safe(page)
            await expect(
                page.locator(".cultivation-card").filter(has_text=first_proposal_id)
            ).to_be_visible()
            await expect(page.get_by_text(_WARNING, exact=True)).to_have_count(2)
            await stale_card.scroll_into_view_if_needed()
            await page.screenshot(
                path=temporary / "08-stale-and-cultivation-390.png",
                full_page=True,
            )
        finally:
            await browser.close()

    forbidden_body_keys = {
        "tenant_id",
        "actor",
        "actor_id",
        "facts_hash",
        "provenance",
        "owner",
        "owner_employee",
        "approver",
        "decided_by",
    }
    assert len(approval_request_bodies) == 2
    assert all(body == {"decision": "approve"} for body in approval_request_bodies)
    assert not forbidden_body_keys.intersection(_all_keys(_POLICY_BODY))
    assert all(
        not forbidden_body_keys.intersection(_all_keys(body))
        for body in approval_request_bodies
    )
    assert not console_issues, console_issues
    assert not page_errors, page_errors
    assert not http_issues, http_issues
    assert await _side_effect_counts(stack) == side_effects_before
    assert stack.controls.tavily_usage_calls == 0
    assert stack.controls.tavily_search_calls == 0
    assert stack.controls.page_validate_calls == 0
    assert stack.controls.page_fetch_calls == 0
    assert stack.controls.model_calls == 0
    assert stack.controls.real_network_calls == 0
    async with stack.factory() as session:
        catalog_runs = dict(
            (
                await session.execute(
                    select(WorkflowRunRow.workflow_type, func.count())
                    .where(
                        WorkflowRunRow.tenant_id == str(stack.tenant_id),
                        WorkflowRunRow.workflow_type.in_(
                            (
                                "catalog_proposal_policy_change",
                                "catalog_cluster_evaluation",
                                "catalog_product_cultivation",
                            )
                        ),
                    )
                    .group_by(WorkflowRunRow.workflow_type)
                )
            ).all()
        )
        dead_catalog_events = [
            event_type
            for event_type in (
                await session.scalars(
                    select(OutboxEventRow.event_type).where(
                        OutboxEventRow.tenant_id == str(stack.tenant_id),
                        OutboxEventRow.status == "dead",
                        OutboxEventRow.event_type.in_(
                            (
                                "NeedClusterMembershipChanged",
                                "AccountCountryFactsChanged",
                                "CatalogProposalPolicyActivated",
                                "CatalogProductProposalCreated",
                                "ApprovalDecided",
                            )
                        ),
                    )
                )
            ).all()
        ]
    assert catalog_runs == {
        "catalog_proposal_policy_change": 1,
        "catalog_cluster_evaluation": 3,
        "catalog_product_cultivation": 3,
    }
    assert dead_catalog_events == []


def _publish_final_screenshots(temporary: Path) -> tuple[str, ...]:
    screenshots = tuple(sorted(temporary.glob("*.png")))
    assert len(screenshots) == 8
    _OUTPUT.mkdir(parents=True, exist_ok=True)
    existing = tuple(_OUTPUT.iterdir())
    assert all(artifact.is_file() or artifact.is_symlink() for artifact in existing)
    for artifact in existing:
        artifact.unlink(missing_ok=True)
    for screenshot in screenshots:
        shutil.copyfile(screenshot, _OUTPUT / screenshot.name)
    for sidecar in _OUTPUT.glob("._*"):
        sidecar.unlink(missing_ok=True)
    return tuple(screenshot.name for screenshot in screenshots)


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_catalog_product_proposal_controlled_real_core() -> None:
    """批准只入培养队列；重放、重启、陈旧事实与外部副作用均失败关闭。"""

    with TemporaryDirectory(prefix="tradeos-catalog-acceptance-") as directory:
        async for stack in e2e_stack_lifecycle():
            await _run_controlled_acceptance(stack, Path(directory))
            assert _publish_final_screenshots(Path(directory)) == (
                "01-no-policy-desktop.png",
                "02-no-policy-390.png",
                "03-policy-pending-desktop.png",
                "04-policy-approval-deep-link.png",
                "05-active-policy-proposal-desktop.png",
                "06-active-policy-proposal-390.png",
                "07-queued-cultivation-desktop.png",
                "08-stale-and-cultivation-390.png",
            )
            return
    raise AssertionError("Catalog Product Proposal 受控栈未启动")
