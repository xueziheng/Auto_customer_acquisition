"""Phase 1 用户可见闭环：真实 PostgreSQL、Uvicorn、Vite 与 Chromium。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING

import pytest
from fastapi.encoders import jsonable_encoder
from playwright.async_api import Route, async_playwright, expect
from sqlalchemy import select

from agent_runtime.account_discovery.agent import AccountDiscoveryAgent
from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.qualification_agent.agent import QualificationAgent
from apps.api.composition.runtime import build_phase1_dependencies
from apps.scheduler_worker.account_discovery import (
    BossAccountDiscoveryActorResolver,
    DemandAccountDiscoveryTaskReader,
)
from apps.scheduler_worker.adapters.message_content_reader import (
    ArtifactMessageContentReader,
)
from apps.scheduler_worker.adapters.reply_business_facts import (
    TenantBoundReplyBusinessFactsReader,
)
from apps.scheduler_worker.adapters.reply_customer_evidence import (
    TenantBoundCustomerReplyEvidenceVerifier,
)
from apps.scheduler_worker.adapters.reply_evidence_reader import (
    ConversationReplyEvidenceReader,
)
from apps.scheduler_worker.adapters.reply_opportunity_intake import (
    DurableReplyOpportunityIntake,
)
from apps.scheduler_worker.directive_reader import DirectiveDemandDiscoveryTaskReader
from apps.scheduler_worker.reply_actions import ComposedReplyActionPorts
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from domains.approvals.service import ApprovalType, BlastRadius
from domains.demand.service_impl import DemandServiceImpl
from domains.directives.schemas import (
    DemandDiscoveryPlanInput,
    DiscoverySearchQueryInput,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.opportunities.permissions import ScopeLevel as OpportunityScopeLevel
from domains.opportunities.schemas import (
    HandoffCreateRequest,
    OpportunityCreateRequest,
    ValidatedNeedEvidence,
)
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
)
from domains.organization.schemas import PlaybookApprovalFact, PlaybookProposalCreate
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignCreateRequest,
    DeliveryCorrelationBinding,
    OutreachSenderRole,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    StepIntent,
)
from domains.outreach.service_impl import OutreachServiceImpl
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from infra.db.tables import (
    DemandSignalRow,
    HandoffRow,
    NeedHypothesisRow,
    OpportunityRow,
    OutreachEnrollmentRow,
    RawArtifactRow,
    ValidatedNeedRow,
)
from infra.secrets import EnvironmentSecretResolver
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType
from tests.integration.test_phase1_closed_loop import (
    BODY,
    EMAIL,
    NOW,
    PAGE_TEXT,
    REPLY_BYTES,
    _ControlledAccountModel,
    _ControlledDemandModel,
    _ControlledDemandPageReader,
    _ControlledDemandSearcher,
    _ControlledEnricher,
    _ControlledReplyModel,
    _ControlledTransport,
    _ControlledVerifier,
    _MemoryBlobTransport,
    _NoReply,
    _poll_until_idle,
    _ProspectingContactEligibility,
    _ProspectingOrganizationFacts,
    _StableHasher,
    _UnusedSendingIdentities,
)
from tests.outreach_fakes import FakeApprovals, FakeAudit, FakeSenders, Trace
from workflows.account_discovery.flow import (
    build_account_discovery_definition,
    build_account_discovery_handlers,
)
from workflows.demand_discovery.flow import (
    build_demand_discovery_definition,
    build_demand_discovery_handlers,
)
from workflows.reply_qualification.flow import (
    build_reply_qualification_definition,
    build_reply_qualification_handlers,
)

if TYPE_CHECKING:
    from conftest import E2EStack


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class _CapturedAccountQueue:
    def __init__(self) -> None:
        self.hypothesis_id: str | None = None

    async def start(
        self,
        tenant_id: TenantId,
        hypothesis_id: object,
        **kwargs: object,
    ) -> str:
        del tenant_id, kwargs
        self.hypothesis_id = str(hypothesis_id)
        return "run_browser_account_pending"


def _json(value: object) -> object:
    return jsonable_encoder(value)


async def _seed_ordering_sentinel(
    *,
    stack: E2EStack,
    opportunities: object,
    actor: OpportunityActor,
    clock: _Clock,
) -> str:
    """只创建排序哨兵；后续断言仍精确绑定回复闭环生成的三类 ID。"""
    provenance = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="msg-ordering-sentinel",
        extracted_by="human",
        extracted_at=clock.now(),
    )
    opportunity_id = await opportunities.create_from_need(
        stack.tenant_id,
        OpportunityCreateRequest(
            need_id="need-ordering-sentinel",
            account_id="acc-ordering-sentinel",
            account_name="排序哨兵企业",
            country="US",
            product_category="hardware",
            evidence_tier="customer_interest_reply",
            has_verified_contact=True,
            category_allowed=True,
            minimum_order_value=Money(Decimal(1000), CurrencyCode("USD")),
            supply_available=True,
            estimated_order_value=Money(Decimal(5000), CurrencyCode("USD")),
            field_provenance={"account_name": provenance, "country": provenance},
        ),
        ValidatedNeedEvidence(EvidenceLevel.CUSTOMER_INTEREST_REPLY, provenance),
        actor=actor,
    )
    assert opportunity_id is not None
    await opportunities.assign(
        stack.tenant_id,
        opportunity_id,
        stack.employees.sales_a,
        stack.employees.boss,
        actor=actor,
    )
    handoff_id = await opportunities.request_handoff(
        stack.tenant_id,
        HandoffCreateRequest(
            opportunity_id=str(opportunity_id),
            trigger="quote_requested",
            account_name="排序哨兵企业",
            country="US",
            why_valuable="仅用于证明最久等待优先的受控排序哨兵",
            customer_verbatim="Please quote the controlled ordering fixture.",
            customer_verbatim_provenance=provenance,
            validated_need_summary="受控排序哨兵",
            suggested_next_step="先处理最早进入队列的项目",
            evidence_links=["artifact:ordering-sentinel"],
        ),
        actor=actor,
    )
    return str(handoff_id)


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_phase1_browser_visible_reply_to_handoff_chain(
    e2e_stack: E2EStack,
) -> None:
    stack = e2e_stack
    tenant = TenantId(stack.tenant_id)
    clock = _Clock(NOW)
    dependencies = build_phase1_dependencies(
        stack.runtime_settings,
        stack.factory,
        now=clock.now,
        secret_resolver=EnvironmentSecretResolver(
            {
                stack.runtime_settings.tool_call_fingerprint_key_ref: "f" * 32,
                stack.runtime_settings.gmail_oauth_token_ref: "not-used",
                stack.runtime_settings.openai_api_key_ref: "not-used",
                **{
                    reference.secret_ref: "u" * 32
                    for reference in stack.runtime_settings.unsubscribe_key_refs
                },
            }
        ),
    )
    assert dependencies.directives is not None
    assert dependencies.approvals is not None
    assert dependencies.organization is not None
    assert dependencies.conversations is not None

    blob_transport = _MemoryBlobTransport()
    raw_store = RawArtifactStoreImpl(
        lambda bound: SqlAlchemyArtifactUnitOfWork(stack.factory, bound),
        blob_transport,
        1_000_000,
        clock.now,
        new_id,
    )
    prospecting = ProspectingServiceImpl(
        lambda bound: SqlAlchemyProspectingUnitOfWork(
            stack.factory, bound, now=clock.now
        ),
        _StableHasher(),
        now=clock.now,
    )
    demand = DemandServiceImpl(
        lambda bound: SqlAlchemyDemandUnitOfWork(stack.factory, bound, now=clock.now),
        now=clock.now,
        account_names=_ProspectingOrganizationFacts(prospecting),
        customer_evidence=None,
    )
    sender_id = SendingIdentityId(new_id("sid"))
    campaign_id = CampaignId(new_id("cmp"))
    trace = Trace()
    approval_snapshots = FakeApprovals(trace)
    senders = FakeSenders(
        {
            sender_id: SendingIdentityEligibilitySnapshot(
                tenant,
                sender_id,
                OutreachSenderRole.COLD_OUTREACH,
                True,
                True,
                100,
                clock.now(),
            )
        },
        trace,
    )
    outreach = OutreachServiceImpl(
        lambda bound: SqlAlchemyOutreachUnitOfWork(stack.factory, bound, now=clock.now),
        _ProspectingContactEligibility(prospecting),
        senders,
        approval_snapshots,
        _NoReply(),
        dependencies.outreach_authorizer,
        FakeAudit(trace),
        now=clock.now,
    )
    outreach_actor = OutreachActor(
        str(stack.employees.boss),
        OutreachScope(level=OutreachScopeLevel.TENANT),
        "boss",
    )
    campaign = await outreach.create_campaign(
        tenant,
        CampaignCreateRequest(
            name="Phase 1 Browser Controlled Campaign",
            markets=("US",),
            target_entity_types=("importer",),
            allowed_categories=("hardware",),
            sender_identity_ids=(sender_id,),
            steps=(SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
            daily_new_contact_limit=5,
            daily_total_message_limit=10,
            handoff_triggers=("quote_requested",),
        ),
        actor=outreach_actor,
    )
    assert campaign.campaign_id != campaign_id
    campaign_id = campaign.campaign_id
    campaign = await outreach.submit_campaign(tenant, campaign_id, actor=outreach_actor)
    approval_id = await dependencies.approvals.submit(
        tenant,
        ApprovalType.CAMPAIGN_BOUNDARY_CHANGE,
        f"批准 Campaign：{campaign.name}（版本 {campaign.version}）",
        {
            "campaign_id": str(campaign_id),
            "version": campaign.version,
            "allowed_categories": ["hardware"],
            "sender_identity_ids": [str(sender_id)],
        },
        "Campaign 边界必须由另一名授权员工确认，且只批准该精确版本。",
        BlastRadius(
            affected_entities=[f"Campaign {campaign_id} 版本 {campaign.version}"],
            if_approved="允许激活该精确版本。",
            if_rejected="保持待审批，不会发送。",
            reversible=True,
        ),
        proposed_by_employee=stack.employees.manager,
        change_set_ref=f"campaign:{campaign_id}:v{campaign.version}",
        owner_employee=stack.employees.manager,
    )

    proposal_id = await dependencies.directives.submit_discovery_proposal(
        tenant,
        "Find verified US hardware demand with one bounded public search.",
        DemandDiscoveryPlanInput(
            objective="探索 US hardware 公开需求信号",
            queries=(
                DiscoverySearchQueryInput(
                    "US hardware distribution expansion", "US", "hardware", 1
                ),
            ),
            target_countries=("US",),
            target_categories=("hardware",),
            excluded_countries=(),
            excluded_categories=(),
            max_search_queries=1,
            max_pages_read=1,
            max_signals=1,
            max_hypotheses=1,
            minimum_confidence_tier="low_mid",
            strategy_group="company_change",
            campaign_id=str(campaign_id),
            role_hints=("procurement",),
            assessment_ref="phase1-browser-lia",
        ),
        "只在页面列明的范围与数量上限内形成一条待验证需求假设。",
        ["执行最多一次公开检索", "形成最多一条需求假设"],
        "controlled-directive-parser-v1",
    )
    proposal = await dependencies.directives.get_proposal(tenant, proposal_id)

    demand = DemandServiceImpl(
        lambda bound: SqlAlchemyDemandUnitOfWork(stack.factory, bound, now=clock.now),
        now=clock.now,
        account_names=_ProspectingOrganizationFacts(prospecting),
        customer_evidence=TenantBoundCustomerReplyEvidenceVerifier(
            tenant_id=tenant,
            conversations_uow_factory=lambda bound: SqlAlchemyConversationsUnitOfWork(
                stack.factory, bound, now=clock.now
            ),
            outreach=outreach,
        ),
    )
    captured_account_queue = _CapturedAccountQueue()
    demand_handlers = build_demand_discovery_handlers(
        task_reader=DirectiveDemandDiscoveryTaskReader(dependencies.directives),
        searcher=_ControlledDemandSearcher(),
        page_reader=_ControlledDemandPageReader(raw_store),
        capability=DemandIntelligenceAgent(
            "controlled-discovery-model-v1",
            _ControlledDemandModel(),
            object(),
            CredentialMarkerGuard(),
        ),
        demand=demand,
        prospecting=prospecting,
        account_queue=captured_account_queue,
    )
    from infra.db.workflow_engine import PostgresWorkflowEngine

    demand_engine = PostgresWorkflowEngine(
        stack.factory, demand_handlers, now=clock.now
    )
    demand_engine.register(build_demand_discovery_definition())

    opportunity_actor = OpportunityActor(
        str(stack.employees.boss),
        OpportunityScope(level=OpportunityScopeLevel.TENANT),
        "boss",
    )
    sentinel_handoff_id = await _seed_ordering_sentinel(
        stack=stack,
        opportunities=dependencies.opportunities,
        actor=opportunity_actor,
        clock=clock,
    )
    clock.advance(timedelta(hours=1))

    evidence_dir = Path(gettempdir()) / f"tradeos-task-6c-{new_id('run')}"
    evidence_dir.mkdir(parents=True)
    console_issues: list[str] = []

    async with dependencies.employees(tenant) as employees:  # noqa: SIM117
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            page.on(
                "console",
                lambda message: (
                    console_issues.append(f"{message.type}: {message.text}")
                    if message.type in {"error", "warning"}
                    else None
                ),
            )

            async def proposal_route(route: Route) -> None:
                await route.fulfill(status=200, json=_json(proposal))

            confirmed_run_ids: list[str] = []

            async def confirmation_route(route: Route) -> None:
                await dependencies.directives.confirm_proposal(
                    tenant, proposal_id, stack.employees.boss
                )
                active = await dependencies.directives.get_active(tenant)
                assert active is not None
                run_id = await demand_engine.start(
                    tenant,
                    "demand_discovery",
                    str(proposal_id),
                    {
                        "proposal_id": str(proposal_id),
                        "acting_user_id": str(stack.employees.boss),
                    },
                    f"demand-discovery:{proposal_id}",
                )
                await _poll_until_idle(demand_engine, tenant)
                confirmed_run_ids.append(str(run_id))
                await route.fulfill(
                    status=200,
                    json={
                        "proposal_id": str(proposal_id),
                        "directive_id": str(active.directive_id),
                        "run_id": str(run_id),
                        "workflow_type": "demand_discovery",
                    },
                )

            await page.route(
                f"{stack.api_origin}/commands/discovery-proposals", proposal_route
            )
            await page.goto(f"{stack.web_origin}/commands")
            await expect(page).to_have_title("TradeOS")
            await expect(page.get_by_role("heading", name="指挥中心")).to_be_visible()
            await page.get_by_label("老板原始指令").fill(proposal.raw_text)
            await page.get_by_role("button", name="生成待确认提案").click()
            await expect(page.get_by_text(str(proposal_id), exact=True)).to_be_visible()
            await expect(page.get_by_text("工作流不得越过")).to_be_visible()
            await page.unroute(
                f"{stack.api_origin}/commands/discovery-proposals", proposal_route
            )
            confirmation_url = (
                f"{stack.api_origin}/commands/discovery-proposals/{proposal_id}/confirm"
            )
            await page.route(confirmation_url, confirmation_route)
            await page.get_by_role("button", name="确认并启动").click()
            await expect(page.get_by_text("受限工作流已启动")).to_be_visible()
            await page.unroute(confirmation_url, confirmation_route)
            await page.screenshot(path=evidence_dir / "01-command-confirmed.png")

            assert len(confirmed_run_ids) == 1
            confirmed_run_id = confirmed_run_ids[0]
            async with stack.factory() as session:
                hypothesis_row = (
                    await session.execute(
                        select(NeedHypothesisRow).where(
                            NeedHypothesisRow.tenant_id == tenant
                        )
                    )
                ).scalar_one()
                signal_row = (
                    await session.execute(
                        select(DemandSignalRow).where(
                            DemandSignalRow.tenant_id == tenant
                        )
                    )
                ).scalar_one()
            hypothesis_id = hypothesis_row.hypothesis_id
            account_id = hypothesis_row.account_id
            signal_id = signal_row.signal_id
            account_name = (await prospecting.get_account(tenant, account_id)).name
            assert captured_account_queue.hypothesis_id == str(hypothesis_id)
            receipt = page.get_by_text(f"Run {confirmed_run_id}", exact=False)
            await expect(receipt).to_be_visible()

            account_handlers = build_account_discovery_handlers(
                task_reader=DemandAccountDiscoveryTaskReader(
                    demand, allowed_countries=("US",)
                ),
                capability=AccountDiscoveryAgent(
                    "controlled-account-model",
                    _ControlledAccountModel(),
                    object(),
                    CredentialMarkerGuard(),
                ),
                prospecting=prospecting,
                enricher=_ControlledEnricher(),
                verifier=_ControlledVerifier(),
                employees=employees,
                outreach=outreach,
                actor_resolver=BossAccountDiscoveryActorResolver(employees),
                now=clock.now,
            )
            account_engine = PostgresWorkflowEngine(
                stack.factory, account_handlers, now=clock.now
            )
            account_engine.register(build_account_discovery_definition())

            await page.goto(f"{stack.web_origin}/demand")
            await expect(page.get_by_role("heading", name="需求雷达")).to_be_visible()
            await expect(page.get_by_text(PAGE_TEXT)).to_be_visible()
            await page.get_by_role("button", name="需求假设").click()
            await expect(page.get_by_text("推断", exact=True).first).to_be_visible()
            await expect(page.get_by_text("置信档位：low_mid")).to_be_visible()
            assert "置信档位：0." not in await page.locator("body").inner_text()
            await page.screenshot(path=evidence_dir / "02-demand-evidence.png")

            async def activate_route(route: Route) -> None:
                approval_snapshots.values[(campaign_id, campaign.version)] = (
                    CampaignApprovalSnapshot(
                        tenant,
                        campaign_id,
                        campaign.version,
                        ApprovalId(str(approval_id)),
                        CampaignApprovalState.APPROVED,
                        stack.employees.boss,
                        clock.now(),
                    )
                )
                active = await outreach.activate_campaign(
                    tenant, campaign_id, actor=outreach_actor
                )
                await route.fulfill(status=200, json=_json(active))

            await page.goto(f"{stack.web_origin}/approvals")
            await expect(page.get_by_text(str(approval_id), exact=True)).to_be_visible()
            await expect(
                page.get_by_text(f"Campaign {campaign_id} 版本 1")
            ).to_be_visible()
            await page.get_by_role("button", name="批准此精确变更").click()
            await expect(page.get_by_text(str(approval_id), exact=True)).to_have_count(
                0
            )

            activation_url = f"{stack.api_origin}/crm/campaigns/{campaign_id}/activate"
            await page.route(activation_url, activate_route)
            await page.goto(f"{stack.web_origin}/campaigns")
            await expect(page.get_by_text("不可变版本 v1")).to_be_visible()
            await expect(
                page.get_by_text("暂停只阻止新发送；入站回复仍继续处理。")
            ).to_be_visible()
            await page.get_by_role("button", name="激活已批准版本").click()
            await expect(page.get_by_text("Campaign 精确版本已激活。")).to_be_visible()
            await page.unroute(activation_url, activate_route)

            async def discovery_route(route: Route) -> None:
                payload = route.request.post_data_json
                assert set(payload) == {
                    "hypothesis_id",
                    "campaign_id",
                    "role_hints",
                    "assessment_ref",
                }
                run_id = await account_engine.start(
                    tenant,
                    "account_discovery",
                    str(hypothesis_id),
                    {
                        **payload,
                        "acting_user_id": str(stack.employees.boss),
                    },
                    f"account-discovery:{hypothesis_id}:{campaign_id}",
                )
                await _poll_until_idle(account_engine, tenant)
                await route.fulfill(
                    status=200,
                    json={
                        "run_id": str(run_id),
                        "workflow_type": "account_discovery",
                        "subject_ref": str(hypothesis_id),
                    },
                )

            discovery_url = f"{stack.api_origin}/prospects/discoveries"
            await page.route(discovery_url, discovery_route)
            await page.goto(f"{stack.web_origin}/prospects/accounts")
            await page.get_by_label("需求假设 ID").fill(str(hypothesis_id))
            await page.get_by_label("Campaign ID").fill(str(campaign_id))
            await page.get_by_label("联系人角色线索").fill("procurement")
            await page.get_by_label("正当利益评估引用").fill("phase1-browser-lia")
            await page.get_by_role("button", name="启动账户发现").click()
            await expect(
                page.get_by_text("账户发现任务已创建", exact=False)
            ).to_be_visible()
            await page.get_by_role("button", name="刷新").first.click()
            account_row = page.locator(".account-list li").filter(has_text=account_name)
            await account_row.focus()
            await account_row.press("Enter")
            await expect(page.get_by_text("已验证，可入组", exact=True)).to_be_visible()
            await expect(
                page.get_by_text("legitimate_interest", exact=False)
            ).to_be_visible()
            await expect(page.get_by_text(signal_id, exact=True)).to_be_visible()
            await page.unroute(discovery_url, discovery_route)
            await page.screenshot(path=evidence_dir / "03-discovery-enrollment.png")

            await page.goto(f"{stack.web_origin}/campaigns")
            await expect(page.get_by_text("Campaign v1", exact=True)).to_be_visible()
            await expect(page.get_by_text("enrolled", exact=True).first).to_be_visible()

            async with stack.factory() as session:
                enrollment = (
                    await session.execute(
                        select(OutreachEnrollmentRow).where(
                            OutreachEnrollmentRow.tenant_id == tenant,
                            OutreachEnrollmentRow.source_hypothesis_id
                            == str(hypothesis_id),
                        )
                    )
                ).scalar_one()

            send_actor = OutreachActor(
                "system:phase1-browser-send",
                OutreachScope(
                    level=OutreachScopeLevel.SYSTEM,
                    allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
                ),
                "system",
            )
            attempt = await outreach.prepare_message_attempt(
                tenant, enrollment.enrollment_id, actor=send_actor
            )
            binding = DeliveryCorrelationBinding(
                deterministic_message_id=(
                    f"<phase1-browser-route.{'d' * 64}@messages.tradeos.invalid>"
                ),
                idempotency_header=f"phase1-browser-route.{'d' * 64}",
                route_id="phase1-browser-route",
            )
            bound_attempt = await outreach.bind_delivery_correlation(
                tenant,
                attempt.attempt_id,
                binding,
                actor=OutreachActor(
                    "system:phase1-browser-send",
                    OutreachScope(
                        level=OutreachScopeLevel.SYSTEM,
                        allowed_attempt_ids=frozenset({attempt.attempt_id}),
                    ),
                    "system",
                ),
            )
            await outreach.claim_message_send(
                tenant, attempt.attempt_id, actor=send_actor
            )
            provider_ref = await _ControlledTransport().send(bound_attempt)
            await outreach.record_sent(
                tenant, attempt.attempt_id, provider_ref, actor=send_actor
            )
            outbound_id = OutboundMessageId(bound_attempt.deterministic_message_id)

            reply_meta = await raw_store.put(
                tenant, RawArtifactKind.EMAIL_RAW, REPLY_BYTES, "message/rfc822"
            )
            message_id = await dependencies.conversations.ingest_inbound(
                tenant,
                None,
                ProspectAccountId(enrollment.account_id),
                reply_meta.artifact_id,
                "<phase1-browser-reply@example.test>",
                clock.now(),
                outbound_message_id=outbound_id,
            )

            organization_actor = OrganizationActor(
                str(stack.employees.boss),
                OrganizationScope(OrganizationScopeLevel.TENANT, tenant),
                "boss",
            )
            proposed = await dependencies.organization.propose_playbook(
                tenant,
                PlaybookProposalCreate.model_validate(
                    {
                        "company_type": "trading_company",
                        "minimum_deal_amount": "1000",
                        "minimum_deal_currency": "USD",
                        "excluded_categories": [],
                        "sourcing_regions": ["guangdong"],
                        "excluded_countries": [],
                    }
                ),
                actor=organization_actor,
                idempotency_key=f"phase1-browser-playbook:{tenant}",
            )
            playbook = await dependencies.organization.get_version(
                tenant, proposed.playbook_version_id, actor=organization_actor
            )
            await dependencies.organization.activate_playbook(
                tenant,
                playbook.playbook_version_id,
                PlaybookApprovalFact(
                    approval_id=ApprovalId(new_id("apr")),
                    approval_type="playbook_change",
                    change_set_ref=playbook.change_set_ref,
                    decided_by=stack.employees.boss,
                    decided_at=clock.now(),
                ),
                actor=OrganizationActor(
                    "system:phase1-browser-playbook",
                    OrganizationScope(OrganizationScopeLevel.SYSTEM, tenant),
                    "system",
                ),
            )

            content = ArtifactMessageContentReader(
                lambda bound: SqlAlchemyConversationsUnitOfWork(
                    stack.factory, bound, now=clock.now
                ),
                raw_store,
                max_raw_bytes=100_000,
                max_subject_chars=500,
                max_body_chars=10_000,
            )
            action_ports = ComposedReplyActionPorts(
                tenant_id=tenant,
                evidence=ConversationReplyEvidenceReader(
                    lambda bound: SqlAlchemyConversationsUnitOfWork(
                        stack.factory, bound, now=clock.now
                    )
                ),
                business=TenantBoundReplyBusinessFactsReader(
                    tenant_id=tenant,
                    outreach=outreach,
                    demand=demand,
                    prospecting=prospecting,
                    opportunities=dependencies.opportunities,
                    opportunity_actor=opportunity_actor,
                ),
                content=content,
                demand=demand,
                opportunities=dependencies.opportunities,
                outreach=outreach,
                sending_identities=_UnusedSendingIdentities(),
                conversations=dependencies.conversations,
                opportunity_intake=DurableReplyOpportunityIntake(
                    tenant_id=tenant,
                    demand=demand,
                    prospecting=prospecting,
                    organization=dependencies.organization,
                    opportunities=dependencies.opportunities,
                    employees=employees,
                    organization_actor=organization_actor,
                    opportunity_actor=opportunity_actor,
                    employee_actor=EmployeeActor(
                        str(stack.employees.boss), EmployeeScope.TENANT, "boss"
                    ),
                ),
            )
            reply_handlers = build_reply_qualification_handlers(
                classifier=QualificationAgent(
                    model="controlled-reply-model",
                    model_client=_ControlledReplyModel(),
                    gateway=None,
                    guardrails=None,
                ),
                content_reader=content,
                input_guard=CredentialMarkerGuard(),
                conversations=dependencies.conversations,
                outreach=outreach,
                tenant_id=tenant,
                now=clock.now,
                action_ports=action_ports,
            )
            reply_engine = PostgresWorkflowEngine(
                stack.factory, reply_handlers, now=clock.now
            )
            reply_engine.register(build_reply_qualification_definition())
            await reply_engine.start(
                tenant,
                "reply_qualification",
                str(message_id),
                {
                    "message_id": str(message_id),
                    "outbound_message_id": str(outbound_id),
                    "category": None,
                    "enrollment_id": str(enrollment.enrollment_id),
                    "account_id": str(enrollment.account_id),
                    "contact_point_id": str(enrollment.contact_point_id),
                },
                f"reply:{message_id}",
            )
            await _poll_until_idle(reply_engine, tenant)

            async with stack.factory() as session:
                need = (
                    await session.execute(
                        select(ValidatedNeedRow).where(
                            ValidatedNeedRow.tenant_id == tenant
                        )
                    )
                ).scalar_one()
                opportunity = (
                    await session.execute(
                        select(OpportunityRow).where(
                            OpportunityRow.tenant_id == tenant,
                            OpportunityRow.need_id == need.need_id,
                        )
                    )
                ).scalar_one()
                handoff = (
                    await session.execute(
                        select(HandoffRow).where(
                            HandoffRow.tenant_id == tenant,
                            HandoffRow.opportunity_id == opportunity.opportunity_id,
                        )
                    )
                ).scalar_one()
                hypothesis = await session.get(
                    NeedHypothesisRow, (str(tenant), str(hypothesis_id))
                )
                artifact = await session.get(
                    RawArtifactRow, (str(tenant), str(reply_meta.artifact_id))
                )
            assert hypothesis is not None
            assert hypothesis.validated_need_id == need.need_id
            assert opportunity.need_id == need.need_id
            assert handoff.opportunity_id == opportunity.opportunity_id
            assert artifact is not None and artifact.kind == "email_raw"

            await page.goto(f"{stack.web_origin}/inbox")
            await expect(
                page.get_by_role("heading", name="Smart Inbox")
            ).to_be_visible()
            await expect(page.get_by_text("提供规格", exact=True).first).to_be_visible()
            await expect(page.get_by_text(str(reply_meta.artifact_id))).to_be_visible()
            assert BODY not in await page.locator("body").inner_text()
            assert EMAIL not in await page.locator("body").inner_text()

            await page.goto(f"{stack.web_origin}/demand/needs/{need.need_id}")
            await expect(page.get_by_text(BODY, exact=False).first).to_be_visible()
            await expect(
                page.get_by_text(str(message_id), exact=True).first
            ).to_be_visible()
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            await page.screenshot(path=evidence_dir / "04-need-mobile.png")

            await page.goto(f"{stack.web_origin}/crm/handoffs")
            await expect(
                page.get_by_role("heading", name="人工接管队列")
            ).to_be_visible()
            rows = page.locator(".handoff-card")
            await expect(rows).to_have_count(2)
            assert (
                await rows.nth(0).get_attribute("data-handoff-id")
                == sentinel_handoff_id
            )
            assert await rows.nth(1).get_attribute("data-handoff-id") == str(
                handoff.handoff_id
            )
            await rows.nth(1).focus()
            await rows.nth(1).press("Enter")
            await expect(page.get_by_text(BODY, exact=True)).to_be_visible()
            evidence_button = page.get_by_role("button", name="查看来源").first
            await evidence_button.focus()
            await evidence_button.press("Enter")
            dialog = page.get_by_role("dialog")
            await expect(dialog).to_be_visible()
            assert await dialog.evaluate("el => el.contains(document.activeElement)")
            await page.keyboard.press("Escape")
            await expect(dialog).to_be_hidden()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            await page.screenshot(path=evidence_dir / "05-handoff-mobile.png")

            await browser.close()

    assert not console_issues, console_issues
    persisted = json.dumps(
        {
            "hypothesis_id": str(hypothesis_id),
            "need_id": need.need_id,
            "opportunity_id": opportunity.opportunity_id,
            "handoff_id": handoff.handoff_id,
        }
    )
    assert BODY not in persisted
    assert EMAIL not in persisted
    process_logs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (
            stack.api_process.stdout_path,
            stack.api_process.stderr_path,
            stack.vite_process.stdout_path,
            stack.vite_process.stderr_path,
        )
    )
    assert BODY not in process_logs
    assert EMAIL not in process_logs
    assert stack.runtime_settings.database_url.get_secret_value() not in process_logs
    assert evidence_dir.is_dir()
