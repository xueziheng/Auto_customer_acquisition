"""真实栈验证 Settings 只呈现 durable Hunter readiness，且不触发 Provider。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from playwright.async_api import Page, async_playwright, expect
from sqlalchemy import text

from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyApprovalFact,
    CountryPolicyProposalCreate,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from shared.schemas.identifiers import ApprovalId, IdempotencyKey
from shared.schemas.provenance import SourceType
from tests.e2e.conftest import E2EStack, e2e_stack_lifecycle
from tool_gateway.provider_readiness import (
    ProviderConfiguration,
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
    ProviderReadinessState,
)

_COUNTRY = "Hunter Readiness Synthetic Market"
_PROVIDER_NOT_CONFIGURED = "部署尚未声明 Hunter 安全配置版本。"
_VALIDATION_PENDING = "Hunter 配置已声明，等待人工 Provider 验证。"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def isolated_e2e_stack() -> AsyncIterator[E2EStack]:
    """为 append-only readiness 事实启动独占真实栈，结束时整体销毁。"""
    async for stack in e2e_stack_lifecycle():
        yield stack


async def _assert_no_horizontal_overflow(page: Page) -> None:
    metrics = await page.evaluate(
        """() => ({
            viewport: window.innerWidth,
            document: document.documentElement.scrollWidth,
        })"""
    )
    assert metrics["document"] <= metrics["viewport"]


async def _expect_both_readiness_banners(page: Page, message: str) -> None:
    await expect(
        page.get_by_label("Playbook 联系人补全就绪状态").get_by_text(
            message, exact=True
        )
    ).to_be_visible()
    await expect(
        page.get_by_label("国家政策联系人补全就绪状态").get_by_text(
            message, exact=True
        )
    ).to_be_visible()


async def _seed_allowed_country_policy(stack: E2EStack) -> None:
    now = datetime.now(UTC)
    service = ComplianceServiceImpl(
        lambda tenant: SqlAlchemyComplianceUnitOfWork(
            stack.factory, tenant, now=lambda: now
        ),
        Phase1ComplianceAuthorizer(stack.tenant_id),
        now=lambda: now,
    )
    proposer = ComplianceActor(
        actor_id=str(stack.employees.boss),
        tenant_id=stack.tenant_id,
        scope=ComplianceScope.TENANT,
        role="boss",
    )
    activator = ComplianceActor(
        actor_id="system:hunter-readiness-e2e",
        tenant_id=stack.tenant_id,
        scope=ComplianceScope.SYSTEM,
        role="system",
    )
    command = CountryPolicyProposalCreate.model_validate(
        {
            "country": _COUNTRY,
            "public_research_allowed": True,
            "contact_enrichment_allowed": True,
            "cold_b2b_email_allowed": False,
            "personal_data_basis_required": True,
            "subject_type_affects_judgment": True,
            "contact_type_affects_judgment": True,
            "opt_out_deadline_days": 30,
            "local_representative_required": False,
            "requirements": ["synthetic_human_review"],
            "notes": "Synthetic E2E facts confirmed by an authorized employee.",
            "field_sources": {
                field: {
                    "source_type": SourceType.EMPLOYEE_INPUT,
                    "source_id": f"assessment:hunter-readiness:{field.value}",
                }
                for field in DECISION_FIELDS
            },
        }
    )
    proposal = await service.propose_country_policy(
        stack.tenant_id,
        command,
        actor=proposer,
        idempotency_key=IdempotencyKey("hunter-readiness-e2e-policy"),
    )
    await service.activate_country_policy(
        stack.tenant_id,
        proposal.country_policy_version_id,
        CountryPolicyApprovalFact(
            approval_id=ApprovalId("apr_hunter_readiness_e2e"),
            approval_type="country_policy_change",
            change_set_ref=proposal.change_set_ref,
            decided_by=stack.employees.manager,
            decided_at=now,
        ),
        actor=activator,
    )


async def _declare_safe_hunter_configuration(stack: E2EStack) -> None:
    actor = ProviderReadinessActor(
        actor_id="system:hunter-readiness-e2e",
        tenant_id=stack.tenant_id,
        permissions=frozenset(ProviderReadinessPermission),
    )
    service = ProviderReadinessServiceImpl(
        lambda tenant: SqlAlchemyProviderReadinessUnitOfWork(stack.factory, tenant),
        runtime_actor=actor,
    )
    snapshot = await service.declare_configuration(
        stack.tenant_id,
        ProviderConfiguration.hunter_contacts("e2e-v1", "synthetic-v1"),
        actor=actor,
        idempotency_key=IdempotencyKey("hunter-readiness-e2e-configure"),
    )
    assert snapshot.state is ProviderReadinessState.VALIDATION_NOT_RUN


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_hunter_readiness_changes_both_settings_banners_without_provider_io(
    isolated_e2e_stack: E2EStack,
) -> None:
    """安全配置声明只能改变 durable 展示，不能调用、验证或激活 Hunter。"""
    stack = isolated_e2e_stack
    assert not stack.scheduler_task.done(), "真实 scheduler/outbox 循环未运行"
    await _seed_allowed_country_policy(stack)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        browser_errors: list[str] = []
        hunter_requests: list[str] = []
        page.on(
            "console",
            lambda message: (
                browser_errors.append(f"console:{message.type}")
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: browser_errors.append(type(error).__name__))
        page.on(
            "request",
            lambda request: (
                hunter_requests.append(request.url)
                if "hunter.io" in request.url.lower()
                else None
            ),
        )
        try:
            await page.goto(f"{stack.web_origin}/settings", wait_until="networkidle")
            await _expect_both_readiness_banners(page, _PROVIDER_NOT_CONFIGURED)
            await _assert_no_horizontal_overflow(page)

            await _declare_safe_hunter_configuration(stack)
            await page.reload(wait_until="networkidle")
            await _expect_both_readiness_banners(page, _VALIDATION_PENDING)
            await _assert_no_horizontal_overflow(page)

            await page.set_viewport_size({"width": 390, "height": 844})
            await _expect_both_readiness_banners(page, _VALIDATION_PENDING)
            await _assert_no_horizontal_overflow(page)
            assert browser_errors == []
            assert hunter_requests == []
        finally:
            await context.close()
            await browser.close()

    async with stack.factory() as session:
        forbidden_tool_calls = await session.scalar(
            text(
                "SELECT count(*) FROM tool_calls "
                "WHERE tenant_id=:tenant_id "
                "AND tool_id IN "
                "('provider.hunter.validate','contact.enrich','contact.verify')"
            ),
            {"tenant_id": str(stack.tenant_id)},
        )
    assert forbidden_tool_calls == 0
