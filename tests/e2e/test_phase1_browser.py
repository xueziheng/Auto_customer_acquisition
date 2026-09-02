"""Phase 1 用户可见闭环：真实 PostgreSQL、Uvicorn、Vite 与 Chromium。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from tempfile import gettempdir

import pytest
import pytest_asyncio
from fastapi.encoders import jsonable_encoder
from playwright.async_api import Request, Route, async_playwright, expect
from sqlalchemy import select

from agent_runtime.account_discovery.agent import AccountDiscoveryAgent
from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.qualification_agent.agent import QualificationAgent
from apps.api.composition.runtime import (
    ManualSendComposition,
    build_phase1_dependencies,
)
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
from apps.scheduler_worker.campaign_events import (
    AccountDiscoveryCampaignEventHandlers,
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
    CampaignCreateRequest,
    DeliveryCorrelationBinding,
    OutreachSenderRole,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    StepIntent,
)
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from infra.db.tables import (
    ConversationClassificationCorrectionRow,
    DemandSignalRow,
    HandoffRow,
    NeedHypothesisRow,
    OpportunityRow,
    OutboxEventRow,
    OutreachActionRow,
    OutreachEnrollmentRow,
    RawArtifactRow,
    ValidatedNeedRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from infra.secrets import EnvironmentSecretResolver
from shared.events.catalog import ApprovalDecided, CampaignStateChanged
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
from tests.e2e.conftest import E2EStack, e2e_stack_lifecycle
from tests.integration.test_phase1_closed_loop import (
    PAGE_TEXT,
    _ControlledAccountModel,
    _ControlledDemandModel,
    _ControlledDemandPageReader,
    _ControlledDemandSearcher,
    _ControlledEnricher,
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
from tests.outreach_fakes import FakeSenders, Trace
from workflows.account_discovery.flow import (
    build_account_discovery_definition,
    build_account_discovery_handlers,
)
from workflows.demand_discovery.flow import (
    build_demand_discovery_handlers,
    register_demand_discovery,
)
from workflows.reply_qualification.flow import (
    build_reply_qualification_definition,
    build_reply_qualification_handlers,
)


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def phase1_e2e_stack() -> AsyncIterator[E2EStack]:
    """为会写 append-only 闭环事实的旅程启动独占真实栈。"""
    async for stack in e2e_stack_lifecycle():
        yield stack


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def sync(self) -> None:
        self.value = max(self.value, datetime.now(UTC))


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


CUSTOMER_QUOTE = "We need 5000 hardware kits at USD 2 each for our controlled project."
TAIL_SENTINEL = "RAW-ONLY-TAIL-SENTINEL-6C-9917"
RAW_ONLY_EMAIL = "raw-only-tail@example.test"
RAW_ONLY_CREDENTIAL = "Bearer RAW-ONLY-CREDENTIAL-6C-4F7C2A"
LONG_REPLY_BODY = (
    CUSTOMER_QUOTE
    + "\n"
    + ("Additional controlled operational context without new factual fields. " * 12)
    + f"\n{TAIL_SENTINEL} {RAW_ONLY_EMAIL}"
)


def _reply_bytes() -> bytes:
    message = EmailMessage()
    message["Subject"] = "Hardware requirement"
    message["From"] = "controlled-buyer@example.test"
    message["To"] = "sales@example.test"
    message["X-Raw-Only-Credential"] = RAW_ONLY_CREDENTIAL
    message.set_content(LONG_REPLY_BODY)
    return message.as_bytes()


REPLY_BYTES = _reply_bytes()


class _ControlledLongReplyModel:
    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        del system_prompt
        assert len(message["body"]) > 500
        assert TAIL_SENTINEL in message["body"]
        return json.dumps(
            {
                "category": "provides_specification",
                "candidate_fields": [
                    {
                        "field": "product_category",
                        "value": "hardware",
                        "quote": CUSTOMER_QUOTE,
                    },
                    {
                        "field": "quantity",
                        "value": "5000",
                        "quote": CUSTOMER_QUOTE,
                    },
                    {
                        "field": "target_price",
                        "value": '{"amount":"2","currency":"USD"}',
                        "quote": CUSTOMER_QUOTE,
                    },
                ],
            }
        )


class _UnusedDeliveryMaterials:
    async def resolve(self, *args: object) -> object:
        del args
        raise AssertionError("浏览器验收不调用真实/受控邮件工具发送")


class _NoNetworkGmailTransport:
    async def search(self, **kwargs: object) -> None:
        del kwargs
        raise AssertionError("浏览器验收不访问 Gmail")

    async def send(self, **kwargs: object) -> str:
        del kwargs
        raise AssertionError("浏览器验收不访问 Gmail")


class _NoFallbackCampaignApproval:
    async def get_campaign_approval(self, *args: object) -> None:
        del args


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
    phase1_e2e_stack: E2EStack,
    request: pytest.FixtureRequest,
) -> None:
    stack = phase1_e2e_stack
    in_process_logs: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            in_process_logs.append(
                f"{record.levelname}:{record.name}:{record.getMessage()}"
            )

    capture_handler = _CaptureHandler(level=logging.NOTSET)
    root_logger = logging.getLogger()
    root_logger.addHandler(capture_handler)
    request.addfinalizer(lambda: root_logger.removeHandler(capture_handler))
    tenant = TenantId(stack.tenant_id)
    clock = _Clock(datetime.now(UTC))
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
    sender_id = SendingIdentityId(new_id("sid"))
    campaign_id = CampaignId(new_id("cmp"))
    trace = Trace()
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
    secrets = EnvironmentSecretResolver(
        {
            stack.runtime_settings.tool_call_fingerprint_key_ref: "f" * 32,
            stack.runtime_settings.gmail_oauth_token_ref: "not-used",
            stack.runtime_settings.openai_api_key_ref: "not-used",
            **{
                reference.secret_ref: "u" * 32
                for reference in stack.runtime_settings.unsubscribe_key_refs
            },
        }
    )
    dependencies = build_phase1_dependencies(
        stack.runtime_settings,
        stack.factory,
        now=clock.now,
        manual_send=ManualSendComposition(
            contact_eligibility=_ProspectingContactEligibility(prospecting),
            sending_identity_eligibility=senders,
            campaign_approvals=_NoFallbackCampaignApproval(),
            reply_status=_NoReply(),
            delivery_materials=_UnusedDeliveryMaterials(),
            secret_resolver=secrets,
            gmail_transport=_NoNetworkGmailTransport(),
        ),
        secret_resolver=secrets,
    )
    assert dependencies.directives is not None
    assert dependencies.approvals is not None
    assert dependencies.organization is not None
    assert dependencies.conversations is not None
    outreach = dependencies.outreach
    demand = DemandServiceImpl(
        lambda bound: SqlAlchemyDemandUnitOfWork(stack.factory, bound, now=clock.now),
        now=clock.now,
        account_names=_ProspectingOrganizationFacts(prospecting),
        customer_evidence=None,
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
        page_reader=_ControlledDemandPageReader(raw_store, observed_at=clock.now()),
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
    register_demand_discovery(demand_engine)

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

    evidence_dir = Path(gettempdir()) / f"tradeos-task-6c-{new_id('run')}"
    evidence_dir.mkdir(parents=True)
    console_issues: list[str] = []
    console_records: list[str] = []
    http_issues: list[str] = []
    surface_bodies: list[str] = []

    async with dependencies.employees(tenant) as employees:  # noqa: SIM117
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            def capture_console(message) -> None:
                entry = f"{message.type}: {message.text}"
                console_records.append(entry)
                if message.type in {"error", "warning"}:
                    console_issues.append(entry)

            page.on("console", capture_console)
            page.on(
                "response",
                lambda response: (
                    http_issues.append(f"{response.status} {response.url}")
                    if response.status >= 400
                    else None
                ),
            )

            async def assert_surface(path: str, heading: str) -> None:
                await expect(page).to_have_url(f"{stack.web_origin}{path}")
                await expect(page).to_have_title("TradeOS")
                await expect(
                    page.get_by_role("heading", name=heading).first
                ).to_be_visible()
                body = (await page.locator("body").inner_text()).strip()
                assert len(body) > 40
                surface_bodies.append(body)
                assert (
                    await page.locator(
                        "vite-error-overlay, #vite-error-overlay, .vite-error-overlay"
                    ).count()
                    == 0
                )
                assert not console_issues, {
                    "console": console_issues,
                    "http": http_issues,
                }

            async def proposal_route(route: Route) -> None:
                response = await page.request.get(
                    f"{stack.api_origin}/commands/discovery-proposals/{proposal_id}",
                    headers={
                        "X-Tenant-Id": str(tenant),
                        "X-Employee-Id": str(stack.employees.boss),
                    },
                )
                assert response.status == 200
                await route.fulfill(status=200, json=await response.json())

            await page.route(
                f"{stack.api_origin}/commands/discovery-proposals", proposal_route
            )
            await page.goto(f"{stack.web_origin}/commands")
            await assert_surface("/commands", "指挥中心")
            await page.get_by_label("老板原始指令").fill(proposal.raw_text)
            await page.get_by_role("button", name="生成待确认提案").click()
            await expect(page.get_by_text(str(proposal_id), exact=True)).to_be_visible()
            await expect(page.get_by_text("工作流不得越过")).to_be_visible()
            await page.unroute(
                f"{stack.api_origin}/commands/discovery-proposals", proposal_route
            )
            await page.get_by_role("button", name="确认并启动").click()
            await expect(page.get_by_text("受限工作流已启动")).to_be_visible()
            async with stack.factory() as session:
                demand_run = (
                    await session.execute(
                        select(WorkflowRunRow).where(
                            WorkflowRunRow.tenant_id == str(tenant),
                            WorkflowRunRow.workflow_type == "demand_discovery",
                            WorkflowRunRow.subject_ref == str(proposal_id),
                        )
                    )
                ).scalar_one()
            confirmed_run_id = demand_run.run_id
            assert demand_run.context["proposal_id"] == str(proposal_id)
            clock.sync()
            await _poll_until_idle(demand_engine, tenant)
            await page.screenshot(path=evidence_dir / "01-command-confirmed.png")

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
            await assert_surface("/demand", "需求雷达")
            await expect(page.get_by_text(PAGE_TEXT)).to_be_visible()
            await expect(page.get_by_text(str(signal_id), exact=False)).to_be_visible()
            await page.get_by_role("button", name="需求假设").click()
            await expect(page.get_by_text("推断", exact=True).first).to_be_visible()
            await expect(page.get_by_text("置信档位：low_mid")).to_be_visible()
            await expect(
                page.get_by_text(str(hypothesis_id), exact=False)
            ).to_be_visible()
            await expect(page.get_by_text(str(account_id), exact=False)).to_be_visible()
            evidence_disclosure = page.locator(".hypothesis-card details").first
            await evidence_disclosure.locator("summary").focus()
            await evidence_disclosure.locator("summary").press("Enter")
            await expect(evidence_disclosure).to_have_attribute("open", "")
            assert "置信档位：0." not in await page.locator("body").inner_text()
            await page.screenshot(path=evidence_dir / "02-demand-evidence.png")

            discovery_bodies: list[dict[str, object]] = []

            def capture_discovery(request: Request) -> None:
                if (
                    request.method == "POST"
                    and request.url == f"{stack.api_origin}/prospects/discoveries"
                ):
                    discovery_bodies.append(request.post_data_json)

            page.on("request", capture_discovery)
            await page.goto(f"{stack.web_origin}/prospects/accounts")
            await assert_surface("/prospects/accounts", "客户发现")
            await page.get_by_label("需求假设 ID").fill(str(hypothesis_id))
            await page.get_by_label("Campaign ID").fill(str(campaign_id))
            await page.get_by_label("联系人角色线索").fill("procurement")
            await page.get_by_label("正当利益评估引用").fill("phase1-browser-lia")
            async with page.expect_response(
                f"{stack.api_origin}/prospects/discoveries"
            ) as discovery_response_info:
                await page.get_by_role("button", name="启动账户发现").click()
            discovery_response = await discovery_response_info.value
            assert discovery_response.status == 200, await discovery_response.text()
            await expect(
                page.get_by_text("账户发现任务已创建", exact=False)
            ).to_be_visible()
            assert discovery_bodies == [
                {
                    "hypothesis_id": str(hypothesis_id),
                    "campaign_id": str(campaign_id),
                    "role_hints": ["procurement"],
                    "assessment_ref": "phase1-browser-lia",
                }
            ]
            async with stack.factory() as session:
                account_run = (
                    await session.execute(
                        select(WorkflowRunRow).where(
                            WorkflowRunRow.tenant_id == str(tenant),
                            WorkflowRunRow.workflow_type == "account_discovery",
                            WorkflowRunRow.subject_ref == str(hypothesis_id),
                        )
                    )
                ).scalar_one()
            clock.sync()
            await _poll_until_idle(account_engine, tenant)
            async with stack.factory() as session:
                waiting_run = await session.get(WorkflowRunRow, account_run.run_id)
                waiting_step = (
                    await session.execute(
                        select(WorkflowStepRow).where(
                            WorkflowStepRow.tenant_id == str(tenant),
                            WorkflowStepRow.run_id == account_run.run_id,
                            WorkflowStepRow.step_name == "await_campaign_activation",
                        )
                    )
                ).scalar_one()
                enrollments_before_approval = (
                    (
                        await session.execute(
                            select(OutreachEnrollmentRow).where(
                                OutreachEnrollmentRow.tenant_id == str(tenant)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert waiting_run is not None
            assert waiting_run.current_step == "await_campaign_activation"
            assert waiting_step.status == "waiting_event"
            assert enrollments_before_approval == []
            await page.get_by_role("button", name="刷新").first.click()
            account_row = page.locator(".account-list li").filter(has_text=account_name)
            await account_row.focus()
            await account_row.press("Enter")
            await expect(page.get_by_text("已验证，可入组", exact=True)).to_be_visible()
            await expect(
                page.get_by_text("legitimate_interest", exact=False)
            ).to_be_visible()
            await expect(page.get_by_text(signal_id, exact=True)).to_be_visible()
            await expect(page.get_by_text(str(account_id), exact=True)).to_be_visible()
            await page.screenshot(path=evidence_dir / "03-discovery-waiting.png")

            account_events = AccountDiscoveryCampaignEventHandlers(
                engine=account_engine,
                approvals=dependencies.approvals,
                factory=stack.factory,
                tenant_id=tenant,
            )
            stack.scheduler_runtime.outbox.register_handler(
                CampaignStateChanged,
                "e2e.account_discovery.campaign_state_changed",
                account_events,
            )
            stack.scheduler_runtime.outbox.register_handler(
                ApprovalDecided,
                "e2e.account_discovery.campaign_approval_decided",
                account_events,
            )

            await page.goto(f"{stack.web_origin}/approvals")
            await assert_surface("/approvals", "Approval Center")
            await expect(page.get_by_text(str(approval_id), exact=True)).to_be_visible()
            await expect(
                page.get_by_text(f"Campaign {campaign_id} 版本 1")
            ).to_be_visible()
            await page.get_by_role("button", name="批准此精确变更").click()
            await expect(page.get_by_text(str(approval_id), exact=True)).to_have_count(
                0
            )

            await page.goto(f"{stack.web_origin}/campaigns")
            await assert_surface("/campaigns", "Campaign Center")
            await expect(page.get_by_text("不可变版本 v1")).to_be_visible()
            await expect(
                page.get_by_text("暂停只阻止新发送；入站回复仍继续处理。")
            ).to_be_visible()
            await page.get_by_role("button", name="激活已批准版本").click()
            await expect(page.get_by_text("Campaign 精确版本已激活。")).to_be_visible()
            approval_view = await dependencies.approvals.get(tenant, approval_id)
            assert approval_view.state == "applied"
            assert approval_view.decided_by_employee == stack.employees.boss

            await stack.scheduler_runtime.outbox.drain()
            for _ in range(20):
                await account_engine.poll_due(tenant, 10)
                async with stack.factory() as session:
                    resumed_run = await session.get(
                        WorkflowRunRow, account_run.run_id
                    )
                if resumed_run is not None and resumed_run.status == "completed":
                    break
                await asyncio.sleep(0.25)
            else:
                raise AssertionError("Campaign 激活事件未在有界时间内恢复账户发现")
            async with stack.factory() as session:
                completed_account_run = await session.get(
                    WorkflowRunRow, account_run.run_id
                )
                enrollment = (
                    await session.execute(
                        select(OutreachEnrollmentRow).where(
                            OutreachEnrollmentRow.tenant_id == str(tenant),
                            OutreachEnrollmentRow.source_hypothesis_id
                            == str(hypothesis_id),
                        )
                    )
                ).scalar_one()
            assert completed_account_run is not None
            assert completed_account_run.status == "completed"
            await page.get_by_role("button", name="刷新").first.click()
            await expect(
                page.get_by_text(str(enrollment.enrollment_id), exact=True)
            ).to_be_visible()
            await expect(page.get_by_text(str(account_id), exact=True)).to_be_visible()
            await expect(
                page.get_by_text(str(hypothesis_id), exact=True)
            ).to_be_visible()
            await expect(page.get_by_text("Campaign v1", exact=True)).to_be_visible()
            await expect(page.get_by_text("enrolled", exact=True).first).to_be_visible()

            await page.screenshot(path=evidence_dir / "04-campaign-enrolled.png")

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
                    evidence_reader=ConversationReplyEvidenceReader(
                        lambda bound: SqlAlchemyConversationsUnitOfWork(
                            stack.factory, bound, now=clock.now
                        )
                    ),
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
                    model_client=_ControlledLongReplyModel(),
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
            assert hypothesis.signal_ids == [str(signal_id)]
            assert hypothesis.account_id == str(account_id)
            assert hypothesis.validated_need_id == need.need_id
            assert enrollment.source_hypothesis_id == str(hypothesis_id)
            assert enrollment.account_id == str(account_id)
            assert opportunity.need_id == need.need_id
            assert opportunity.account_id == str(account_id)
            assert handoff.opportunity_id == opportunity.opportunity_id
            assert artifact is not None and artifact.kind == "email_raw"
            _, raw_reply = await raw_store.get(tenant, reply_meta.artifact_id)
            assert len(LONG_REPLY_BODY) > 500
            assert TAIL_SENTINEL.encode() in raw_reply
            assert RAW_ONLY_EMAIL.encode() in raw_reply
            assert RAW_ONLY_CREDENTIAL.encode() in raw_reply

            await page.goto(f"{stack.web_origin}/inbox")
            await assert_surface("/inbox", "Smart Inbox")
            await page.get_by_role("button", name="报价 / 样品 / 规格 1").click()
            await expect(page.get_by_text("提供规格", exact=True).first).to_be_visible()
            await expect(page.get_by_text(str(reply_meta.artifact_id))).to_be_visible()
            await expect(page.get_by_text(str(message_id), exact=True)).to_be_visible()
            await expect(page.get_by_text(str(outbound_id), exact=True)).to_be_visible()
            inbox_body = await page.locator("body").inner_text()
            surface_bodies.append(inbox_body)
            assert TAIL_SENTINEL not in inbox_body
            assert RAW_ONLY_EMAIL not in inbox_body
            assert LONG_REPLY_BODY not in inbox_body
            await page.get_by_label("纠正后的分类").select_option("requests_quote")
            await page.get_by_role("button", name="提交纠正").click()
            await expect(
                page.get_by_text(
                    "人工纠正已记录；模型原判仍保留用于质量评估。",
                    exact=True,
                )
            ).to_be_visible()
            await expect(
                page.get_by_text("人工纠正后：要求报价", exact=True)
            ).to_be_visible()
            await expect(
                page.get_by_text(str(stack.employees.boss), exact=False)
            ).to_be_visible()
            async with stack.factory() as session:
                correction = (
                    await session.execute(
                        select(ConversationClassificationCorrectionRow).where(
                            ConversationClassificationCorrectionRow.tenant_id
                            == str(tenant),
                            ConversationClassificationCorrectionRow.message_id
                            == str(message_id),
                        )
                    )
                ).scalar_one()
            assert correction.corrected_category == "requests_quote"
            assert correction.corrected_by == str(stack.employees.boss)

            await page.goto(f"{stack.web_origin}/demand/needs/{need.need_id}")
            await assert_surface(f"/demand/needs/{need.need_id}", "已验证需求证据链")
            await expect(
                page.get_by_text(CUSTOMER_QUOTE, exact=False).first
            ).to_be_visible()
            await expect(page.get_by_text(need.need_id, exact=False)).to_be_visible()
            await expect(page.get_by_text(str(account_id), exact=False)).to_be_visible()
            provenance_summary = page.locator(".field-provenance summary").first
            await provenance_summary.focus()
            await provenance_summary.press("Enter")
            await expect(provenance_summary.locator("..")).to_have_attribute("open", "")
            await expect(
                page.get_by_text(str(message_id), exact=True).first
            ).to_be_visible()
            assert await provenance_summary.evaluate(
                "element => element === document.activeElement"
            )
            need_body = await page.locator("body").inner_text()
            surface_bodies.append(need_body)
            assert TAIL_SENTINEL not in need_body
            assert RAW_ONLY_EMAIL not in need_body
            assert LONG_REPLY_BODY not in need_body
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            await page.screenshot(path=evidence_dir / "05-need-mobile.png")

            await page.set_viewport_size({"width": 1440, "height": 900})
            await page.goto(f"{stack.web_origin}/crm/opportunities")
            await assert_surface("/crm/opportunities", "机会看板")
            opportunity_card = page.locator(
                f'[data-opportunity-id="{opportunity.opportunity_id}"]'
            )
            await expect(opportunity_card).to_be_visible()
            await opportunity_card.click()
            await expect(opportunity_card).to_have_attribute("aria-current", "true")
            await expect(
                page.get_by_text(str(opportunity.opportunity_id), exact=True)
            ).to_be_visible()
            opportunity_body = await page.locator("body").inner_text()
            surface_bodies.append(opportunity_body)
            assert "演示数据" not in opportunity_body
            assert TAIL_SENTINEL not in opportunity_body
            assert RAW_ONLY_EMAIL not in opportunity_body
            assert RAW_ONLY_CREDENTIAL not in opportunity_body
            assert LONG_REPLY_BODY not in opportunity_body
            await page.screenshot(path=evidence_dir / "06-opportunity.png")

            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto(f"{stack.web_origin}/crm/handoffs")
            await assert_surface("/crm/handoffs", "人工接管队列")
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
            await expect(page.get_by_text(CUSTOMER_QUOTE, exact=True)).to_be_visible()
            await expect(
                page.get_by_text(str(handoff.handoff_id), exact=False)
            ).to_be_visible()
            await expect(
                page.get_by_text(str(opportunity.opportunity_id), exact=False)
            ).to_be_visible()
            await expect(page.get_by_text(need.need_id, exact=False)).to_be_visible()
            await expect(page.get_by_text(str(account_id), exact=False)).to_be_visible()
            handoff_body = await page.locator("body").inner_text()
            surface_bodies.append(handoff_body)
            assert "演示数据" not in handoff_body
            assert TAIL_SENTINEL not in handoff_body
            assert RAW_ONLY_EMAIL not in handoff_body
            assert LONG_REPLY_BODY not in handoff_body
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
            await page.screenshot(path=evidence_dir / "07-handoff-mobile.png")

            await browser.close()

    assert not console_issues, console_issues
    async with stack.factory() as session:
        workflow_contexts = (
            (
                await session.execute(
                    select(WorkflowRunRow.context).where(
                        WorkflowRunRow.tenant_id == str(tenant)
                    )
                )
            )
            .scalars()
            .all()
        )
        outbox_payloads = (
            (
                await session.execute(
                    select(OutboxEventRow.event_payload).where(
                        OutboxEventRow.tenant_id == str(tenant)
                    )
                )
            )
            .scalars()
            .all()
        )
        outreach_ledger = (
            await session.execute(
                select(
                    OutreachActionRow.action_key,
                    OutreachActionRow.action,
                    OutreachActionRow.entity_id,
                    OutreachActionRow.actor_id,
                ).where(OutreachActionRow.tenant_id == str(tenant))
            )
        ).all()
    persisted = json.dumps(
        {
            "proposal_id": str(proposal_id),
            "demand_run_id": confirmed_run_id,
            "account_run_id": account_run.run_id,
            "signal_id": str(signal_id),
            "hypothesis_id": str(hypothesis_id),
            "account_id": str(account_id),
            "enrollment_id": str(enrollment.enrollment_id),
            "outbound_message_id": str(outbound_id),
            "inbound_message_id": str(message_id),
            "need_id": need.need_id,
            "opportunity_id": opportunity.opportunity_id,
            "handoff_id": handoff.handoff_id,
            "workflow_contexts": workflow_contexts,
            "outbox_payloads": outbox_payloads,
            "outreach_ledger": [tuple(row) for row in outreach_ledger],
        },
        default=str,
    )
    for forbidden in (
        TAIL_SENTINEL,
        RAW_ONLY_EMAIL,
        RAW_ONLY_CREDENTIAL,
        LONG_REPLY_BODY,
    ):
        assert forbidden not in persisted
        assert all(forbidden not in body for body in surface_bodies)
        assert forbidden not in "\n".join(console_records)
        assert forbidden not in "\n".join(in_process_logs)
    process_logs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (
            stack.api_process.stdout_path,
            stack.api_process.stderr_path,
            stack.vite_process.stdout_path,
            stack.vite_process.stderr_path,
        )
    )
    for forbidden in (
        TAIL_SENTINEL,
        RAW_ONLY_EMAIL,
        RAW_ONLY_CREDENTIAL,
        LONG_REPLY_BODY,
    ):
        assert forbidden not in process_logs
    assert stack.runtime_settings.database_url.get_secret_value() not in process_logs
    assert evidence_dir.is_dir()
