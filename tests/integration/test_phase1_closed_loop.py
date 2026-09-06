"""Phase 1 后端闭环：真实 PostgreSQL + 公共服务/工作流边界。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial

import pytest
from _pytest.logging import LogCaptureFixture
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.account_discovery.agent import AccountDiscoveryAgent
from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.qualification_agent.agent import QualificationAgent
from apps.composition_support.employee_readers import (
    CurrentEmployeeUserReader,
    employee_service_scope,
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
from apps.scheduler_worker.directive_reader import DirectiveDemandDiscoveryTaskReader
from apps.scheduler_worker.reply_actions import ComposedReplyActionPorts
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from artifact_store.transport import BlobObjectNotFoundError, BlobReadLimitExceeded
from connectors.contact_enrichment.client import (
    ContactCandidate,
    ContactEmailKind,
    ContactEnrichmentResult,
    ContactSource,
    EnrichmentCostNote,
)
from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from connectors.web_search.client import PageSnapshot, WebSearchResult
from domains.directives.schemas import (
    DemandDiscoveryPlanInput,
    DiscoverySearchQueryInput,
)
from domains.directives.service_impl import DirectiveServiceImpl
from domains.employees import models as employee_models
from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    EmployeeScope,
    Phase1EmployeeAuthorizer,
)
from domains.employees.permissions import (
    StandardAuditLogger as EmployeeAuditLogger,
)
from domains.employees.service_impl import EmployeeServiceImpl
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
    Phase1OpportunityAuthorizer,
)
from domains.opportunities.permissions import (
    ScopeLevel as OpportunityScopeLevel,
)
from domains.opportunities.permissions import (
    StandardAuditLogger as OpportunityAuditLogger,
)
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import OpportunityServiceImpl
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
    Phase1OrganizationAuthorizer,
)
from domains.organization.schemas import PlaybookApprovalFact, PlaybookProposalCreate
from domains.organization.service_impl import OrganizationServiceImpl
from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachScope,
    Phase1OutreachAuthorizer,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    DeliveryCorrelationBinding,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
)
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.directive_uow import SqlAlchemyDirectiveUnitOfWork
from infra.db.organization_uow import SqlAlchemyOrganizationUnitOfWork
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.session import create_engine_from
from infra.db.tables import (
    DirectiveProposalRow,
    HandoffRow,
    NeedHypothesisRow,
    OpportunityRow,
    OutboxEventRow,
    OutreachEnrollmentRow,
    RawArtifactRow,
    ValidatedNeedRow,
    WorkflowRunRow,
)
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    EmployeeId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from shared.schemas.money import CurrencyCode, Money
from tests.integration.test_outreach_enrollment_lifecycle import (
    APPROVER as CAMPAIGN_APPROVER,
)
from tests.integration.test_outreach_enrollment_lifecycle import NOW as CAMPAIGN_NOW
from tests.integration.test_outreach_enrollment_lifecycle import (
    _seed_active_campaign,
)
from tests.outreach_fakes import FakeApprovals, FakeAudit, FakeSenders, Trace
from tool_gateway.handlers.web_slots import SearchResultBatch
from workflows.account_discovery.flow import (
    build_account_discovery_definition,
    build_account_discovery_handlers,
)
from workflows.demand_discovery.flow import (
    build_demand_discovery_definition,
    build_demand_discovery_handlers,
)
from workflows.engine.runner import StepStatus
from workflows.reply_qualification.flow import (
    build_reply_qualification_definition,
    build_reply_qualification_handlers,
)
from workflows.reply_qualification.ports import ReplyActionContext

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
BODY = "We need 5000 hardware kits at USD 2 each. CLOSED-LOOP-BODY-7719"
EMAIL = "buyer.closed-loop@example.test"
PAGE_TEXT = (
    "Example.test is headquartered in the US and opened a hardware distribution center."
)
PAGE_BYTES = f"<html><body>{PAGE_TEXT}</body></html>".encode()
PAGE_HASH = hashlib.sha256(PAGE_BYTES).hexdigest()
REPLY_BYTES = (
    "Subject: Hardware requirement\r\n"
    f"From: {EMAIL}\r\n"
    "To: sales@example.test\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    f"{BODY}\r\n"
).encode()


class _StableHasher:
    def fingerprint(self, canonical_value: str) -> str:
        return hashlib.sha256(canonical_value.encode()).hexdigest()


class _DirectiveEmployees:
    def __init__(self, tenant_id: TenantId, boss: EmployeeId) -> None:
        self._tenant_id = tenant_id
        self._boss = boss

    async def is_active_boss(self, tenant_id, employee_id) -> bool:
        return tenant_id == self._tenant_id and employee_id == self._boss

    async def names_for(self, tenant_id, employee_ids):
        if tenant_id != self._tenant_id:
            return {}
        return {item: "Boss" for item in employee_ids if item == self._boss}


class _ProspectingOrganizationFacts:
    def __init__(self, prospecting: object) -> None:
        self._prospecting = prospecting

    async def _accounts(self, tenant_id, account_ids):
        return [
            await self._prospecting.get_account(tenant_id, account_id)
            for account_id in account_ids
        ]

    async def names_for(self, tenant_id, account_ids):
        return {
            item.account_id: item.name
            for item in await self._accounts(tenant_id, account_ids)
        }

    async def countries_for(self, tenant_id, account_ids):
        return {
            item.account_id: item.country
            for item in await self._accounts(tenant_id, account_ids)
        }

    async def domains_for(self, tenant_id, account_ids):
        return {
            item.account_id: item.website_domain
            for item in await self._accounts(tenant_id, account_ids)
            if item.website_domain is not None
        }


class _ControlledAccountModel:
    calls = 0

    async def discover_account(self, *, system_prompt, hypothesis):
        del system_prompt
        self.calls += 1
        return json.dumps(
            {
                "evidence_sufficient": True,
                "source_signal_refs": [hypothesis["source_signal_refs"][0]],
            }
        )


class _MemoryBlobTransport:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, object_key: str, content: bytes) -> None:
        self.objects[object_key] = bytes(content)

    async def get(self, object_key: str) -> bytes:
        try:
            return self.objects[object_key]
        except KeyError:
            raise BlobObjectNotFoundError() from None

    async def get_bounded(self, object_key: str, *, maximum_bytes: int) -> bytes:
        content = await self.get(object_key)
        if len(content) > maximum_bytes:
            raise BlobReadLimitExceeded()
        return content

    async def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)


class _ControlledDemandSearcher:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, tenant_id, run_id, query, country, category, limit):
        del run_id, query, limit
        self.calls += 1
        return SearchResultBatch(
            new_id("wsb"),
            tenant_id,
            country,
            category,
            (WebSearchResult("Expansion", "https://example.test/news", ""),),
        )

    def release(self, batch) -> None:
        del batch

    def discard_all(self) -> None:
        return None


class _ControlledDemandPageReader:
    def __init__(self, store: object, *, observed_at: datetime = NOW) -> None:
        self._store = store
        self._observed_at = observed_at
        self.calls = 0

    async def read_page(self, tenant_id, run_id, batch, result_index):
        del run_id, batch, result_index
        self.calls += 1
        meta = await self._store.put(
            tenant_id,
            RawArtifactKind.WEB_SNAPSHOT,
            PAGE_BYTES,
            "text/html",
        )
        return PageSnapshot(
            PAGE_TEXT,
            "https://example.test/news",
            self._observed_at,
            meta.content_hash,
            meta.artifact_id,
        )


class _ControlledDemandModel:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze_pages(self, *, system_prompt, discovery):
        del system_prompt, discovery
        self.calls += 1
        return json.dumps(
            {
                "signals": [
                    {
                        "signal_type": "product_line_expansion",
                        "source_page_index": 0,
                        "source_excerpt": PAGE_TEXT,
                        "possible_need": "hardware",
                        "evidence_level": "public_company_event",
                    }
                ],
                "hypotheses": [
                    {
                        "account_name_signal_index": 0,
                        "country": "US",
                        "country_signal_index": 0,
                        "category": "hardware",
                        "reasoning": "企业扩张，可能需要 hardware，值得验证",
                        "signal_indexes": [0],
                    }
                ],
            }
        )


class _AccountWorkflowQueue:
    def __init__(self) -> None:
        self.engine: object | None = None

    async def start(
        self,
        tenant_id,
        hypothesis_id,
        *,
        campaign_id,
        acting_user,
        role_hints,
        assessment_ref,
    ):
        if self.engine is None:
            raise AssertionError("account workflow queue 未绑定")
        return await self.engine.start(
            tenant_id,
            "account_discovery",
            str(hypothesis_id),
            {
                "hypothesis_id": str(hypothesis_id),
                "campaign_id": campaign_id,
                "acting_user_id": str(acting_user),
                "role_hints": list(role_hints),
                "assessment_ref": assessment_ref,
            },
            f"closed-loop-account:{hypothesis_id}:{campaign_id}",
        )


class _ControlledEnricher:
    calls = 0

    async def find_contacts(self, tenant_id, hypothesis_id, account_id, role_hints):
        del tenant_id, hypothesis_id, account_id, role_hints
        self.calls += 1
        return ContactEnrichmentResult(
            candidates=(
                ContactCandidate(
                    email=EMAIL,
                    full_name="Controlled Buyer",
                    role_title="Procurement Manager",
                    email_kind=ContactEmailKind.PERSONAL,
                    sources=(
                        ContactSource(
                            uri="https://example.test/contact",
                            first_seen_on=date(2026, 8, 1),
                            last_seen_on=date(2026, 8, 25),
                            still_on_page=True,
                        ),
                    ),
                ),
            ),
            provider="controlled-contact-provider",
            cost_note=EnrichmentCostNote.COUNTED,
        )


class _ControlledVerifier:
    calls = 0

    async def verify(self, tenant_id, contact_point_id):
        del tenant_id, contact_point_id
        self.calls += 1
        return EmailVerificationResult(
            outcome=EmailVerificationOutcome.VERIFIED,
            provider="controlled-verifier",
            checked_at=NOW,
            cost_note=VerificationCostNote.COUNTED,
        )


class _ProspectingContactEligibility:
    def __init__(self, prospecting: object) -> None:
        self._prospecting = prospecting

    async def get_contact_eligibility(
        self, tenant_id, contact_point_id, account_id
    ) -> ContactEligibilitySnapshot:
        detail = await self._prospecting.get_account_detail(tenant_id, account_id)
        point = next(
            item
            for contact in detail.contacts
            for item in contact.contact_points
            if item.contact_point.contact_point_id == contact_point_id
        )
        return ContactEligibilitySnapshot(
            tenant_id=tenant_id,
            contact_point_id=contact_point_id,
            account_id=account_id,
            verification=ContactVerificationStatus(
                point.contact_point.verification.value
            ),
            verified_at=point.contact_point.verified_at,
            legal_basis=ContactLegalBasis(point.legal_basis.value),
            legal_basis_ref=point.assessment_ref or "closed-loop-lia",
            contact_belongs_to_account=True,
            country=detail.account.country,
            entity_type=detail.account.entity_type or "importer",
            qualified_categories=frozenset({"hardware"}),
            observed_at=NOW,
        )


class _NoReply:
    async def get_reply_status(self, tenant_id, contact_point_id, account_id):
        return ReplyStatusSnapshot(
            tenant_id,
            contact_point_id,
            account_id,
            ReplyState.NO_REPLY,
            None,
            NOW,
        )


class _ControlledTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, attempt: object) -> str:
        del attempt
        self.calls += 1
        return "controlled-provider-ref"


class _ControlledReplyModel:
    def __init__(self) -> None:
        self.calls = 0

    async def classify_reply(self, *, system_prompt, message):
        del system_prompt, message
        self.calls += 1
        return json.dumps(
            {
                "category": "provides_specification",
                "candidate_fields": [
                    {"field": "product_category", "value": "hardware", "quote": BODY},
                    {"field": "quantity", "value": "5000", "quote": BODY},
                    {
                        "field": "target_price",
                        "value": '{"amount":"2","currency":"USD"}',
                        "quote": BODY,
                    },
                ],
            }
        )


class _UnusedSendingIdentities:
    async def record_delivery_event(self, *args, **kwargs):
        raise AssertionError("provides_specification 不应写投递反馈")


async def _poll_until_idle(engine: object, tenant_id: TenantId) -> None:
    for _ in range(20):
        if await engine.poll_due(tenant_id, 10) == 0:
            return
    raise AssertionError("workflow 未在有界轮次内结束")


@pytest.mark.asyncio
async def test_phase1_postgres_closed_loop_is_durable_tenant_bound_and_replay_safe(
    db_url: str,
    caplog: LogCaptureFixture,
) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    boss = EmployeeId(new_id("emp"))
    boss_user = UserId(new_id("usr"))
    owner = EmployeeId(new_id("emp"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender_id = SendingIdentityId(new_id("sid"))
    employee_session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        blob_transport = _MemoryBlobTransport()
        raw_store = RawArtifactStoreImpl(
            lambda bound: SqlAlchemyArtifactUnitOfWork(factory, bound),
            blob_transport,
            1_000_000,
            lambda: NOW,
            new_id,
            bounded_transport=blob_transport,
        )
        prospecting = ProspectingServiceImpl(
            lambda bound: SqlAlchemyProspectingUnitOfWork(
                factory, bound, now=lambda: NOW
            ),
            _StableHasher(),
            now=lambda: NOW,
        )

        employees_repo = EmployeeRepositoryImpl(employee_session, tenant)
        await employees_repo.add(
            employee_models.Employee(
                employee_id=boss,
                user_id=boss_user,
                tenant_id=tenant,
                name="Boss",
                role=employee_models.Role.BOSS,
                created_at=NOW,
            )
        )
        await employees_repo.add(
            employee_models.Employee(
                employee_id=owner,
                tenant_id=tenant,
                name="Sales Owner",
                role=employee_models.Role.SALES,
                created_at=NOW,
            )
        )
        await employee_session.commit()
        employees = EmployeeServiceImpl(
            employees=employees_repo,
            territories=TerritoryRepositoryImpl(employee_session, tenant),
            ownership=OwnershipRepositoryImpl(employee_session, tenant),
            now=lambda: NOW,
            manager_pool=lambda _tenant: (),
            count_active_accounts=employees_repo.count_active_accounts,
            authorizer=Phase1EmployeeAuthorizer(tenant),
            audit=EmployeeAuditLogger(),
        )

        await _seed_active_campaign(
            factory,
            tenant,
            campaign_id,
            (sender_id,),
            approval_id,
        )
        trace = Trace()
        approvals = FakeApprovals(trace)
        approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
            tenant,
            campaign_id,
            1,
            approval_id,
            CampaignApprovalState.APPROVED,
            CAMPAIGN_APPROVER,
            CAMPAIGN_NOW,
        )
        senders = FakeSenders(
            {
                sender_id: SendingIdentityEligibilitySnapshot(
                    tenant,
                    sender_id,
                    OutreachSenderRole.COLD_OUTREACH,
                    True,
                    True,
                    100,
                    NOW,
                )
            },
            trace,
        )
        from domains.outreach.service_impl import OutreachServiceImpl

        outreach = OutreachServiceImpl(
            lambda bound: SqlAlchemyOutreachUnitOfWork(factory, bound, now=lambda: NOW),
            _ProspectingContactEligibility(prospecting),
            senders,
            approvals,
            _NoReply(),
            Phase1OutreachAuthorizer(tenant),
            FakeAudit(trace),
            now=lambda: NOW,
        )

        from domains.demand.service_impl import DemandServiceImpl
        from infra.db.workflow_engine import PostgresWorkflowEngine

        demand = DemandServiceImpl(
            lambda bound: SqlAlchemyDemandUnitOfWork(factory, bound, now=lambda: NOW),
            now=lambda: NOW,
            account_names=_ProspectingOrganizationFacts(prospecting),
            customer_evidence=TenantBoundCustomerReplyEvidenceVerifier(
                tenant_id=tenant,
                conversations_uow_factory=lambda bound: (
                    SqlAlchemyConversationsUnitOfWork(factory, bound, now=lambda: NOW)
                ),
                outreach=outreach,
            ),
        )
        directives = DirectiveServiceImpl(
            lambda bound: SqlAlchemyDirectiveUnitOfWork(
                factory, bound, now=lambda: NOW
            ),
            _DirectiveEmployees(tenant, boss),
            now=lambda: NOW,
        )
        proposal_id = await directives.submit_discovery_proposal(
            tenant,
            "Find verified US hardware demand from public company events.",
            DemandDiscoveryPlanInput(
                objective="探索 US hardware 的公开需求信号",
                queries=(
                    DiscoverySearchQueryInput(
                        "US hardware distribution expansion",
                        "US",
                        "hardware",
                        1,
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
                assessment_ref="closed-loop-lia",
            ),
            "在已确认预算内从公开扩张事实形成需求假设并进入账户发现。",
            ["执行一次公开搜索", "最多创建一个需求假设并排队账户发现"],
            "controlled-directive-parser-v1",
        )
        await directives.confirm_proposal(tenant, proposal_id, boss)

        account_model = _ControlledAccountModel()
        enricher = _ControlledEnricher()
        verifier = _ControlledVerifier()
        account_handlers = build_account_discovery_handlers(
            task_reader=DemandAccountDiscoveryTaskReader(
                demand, allowed_countries=("US",)
            ),
            capability=AccountDiscoveryAgent(
                "controlled-account-model",
                account_model,
                object(),
                CredentialMarkerGuard(),
            ),
            prospecting=prospecting,
            enricher=enricher,
            verifier=verifier,
            employees=employees,
            outreach=outreach,
            actor_resolver=BossAccountDiscoveryActorResolver(employees),
            now=lambda: NOW,
        )
        demand_model = _ControlledDemandModel()
        demand_searcher = _ControlledDemandSearcher()
        demand_page_reader = _ControlledDemandPageReader(raw_store)
        account_queue = _AccountWorkflowQueue()
        demand_handlers = build_demand_discovery_handlers(
            task_reader=DirectiveDemandDiscoveryTaskReader(directives, CurrentEmployeeUserReader(
                partial(employee_service_scope, factory, now=lambda: NOW,
                        authorizer=Phase1EmployeeAuthorizer(tenant), audit=EmployeeAuditLogger()),
                EmployeeActor("system:test", EmployeeScope.SYSTEM, "system"))),
            searcher=demand_searcher,
            page_reader=demand_page_reader,
            capability=DemandIntelligenceAgent(
                "controlled-discovery-model-v1",
                demand_model,
                object(),
                CredentialMarkerGuard(),
            ),
            demand=demand,
            prospecting=prospecting,
            account_queue=account_queue,
        )
        discovery_engine = PostgresWorkflowEngine(
            factory, {**demand_handlers, **account_handlers}, now=lambda: NOW
        )
        account_queue.engine = discovery_engine
        discovery_engine.register(build_demand_discovery_definition())
        discovery_engine.register(build_account_discovery_definition())
        discovery_run_id = await discovery_engine.start(
            tenant,
            "demand_discovery",
            proposal_id,
            {
                "proposal_id": proposal_id,
                "acting_user_id": str(boss_user),
            },
            f"closed-loop-demand:{proposal_id}",
        )
        await _poll_until_idle(discovery_engine, tenant)
        await employee_session.commit()

        async with factory() as session:
            discovery_run = await session.get(WorkflowRunRow, discovery_run_id)
            enrollment = (
                await session.execute(
                    select(OutreachEnrollmentRow).where(
                        OutreachEnrollmentRow.tenant_id == tenant
                    )
                )
            ).scalar_one()
            account_run = (
                await session.execute(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.tenant_id == tenant,
                        WorkflowRunRow.workflow_type == "account_discovery",
                    )
                )
            ).scalar_one()
        hypothesis_id = enrollment.source_hypothesis_id
        assert hypothesis_id is not None
        assert discovery_run.status == StepStatus.COMPLETED.value
        assert account_run.status == StepStatus.COMPLETED.value
        assert enrollment.source_hypothesis_id == hypothesis_id
        assert EMAIL not in json.dumps(discovery_run.context)
        assert (
            demand_model.calls,
            demand_searcher.calls,
            demand_page_reader.calls,
        ) == (
            1,
            1,
            1,
        )
        assert (account_model.calls, enricher.calls, verifier.calls) == (1, 1, 1)

        contact_point_id = enrollment.contact_point_id
        send_actor = OutreachActor(
            "system:closed-loop-send",
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
                f"<closed-loop-route.{'d' * 64}@messages.tradeos.invalid>"
            ),
            idempotency_header=f"closed-loop-route.{'d' * 64}",
            route_id="closed-loop-route",
        )
        bound_attempt = await outreach.bind_delivery_correlation(
            tenant,
            attempt.attempt_id,
            binding,
            actor=OutreachActor(
                "system:closed-loop-send",
                OutreachScope(
                    level=OutreachScopeLevel.SYSTEM,
                    allowed_attempt_ids=frozenset({attempt.attempt_id}),
                ),
                "system",
            ),
        )
        await outreach.claim_message_send(tenant, attempt.attempt_id, actor=send_actor)
        transport = _ControlledTransport()
        provider_ref = await transport.send(bound_attempt)
        await outreach.record_sent(
            tenant, attempt.attempt_id, provider_ref, actor=send_actor
        )
        assert transport.calls == 1
        outbound_id = OutboundMessageId(bound_attempt.deterministic_message_id)

        conversations = __import__(
            "domains.conversations.service_impl", fromlist=["ConversationServiceImpl"]
        ).ConversationServiceImpl(
            lambda bound: SqlAlchemyConversationsUnitOfWork(
                factory, bound, now=lambda: NOW
            ),
            now=lambda: NOW,
        )
        reply_meta = await raw_store.put(
            tenant,
            RawArtifactKind.EMAIL_RAW,
            REPLY_BYTES,
            "message/rfc822",
        )
        reply_artifact = reply_meta.artifact_id
        stored_reply_meta, stored_reply_bytes = await raw_store.get(
            tenant, reply_artifact
        )
        assert stored_reply_meta.kind is RawArtifactKind.EMAIL_RAW
        assert stored_reply_bytes == REPLY_BYTES
        message_id = await conversations.ingest_inbound(
            tenant,
            None,
            ProspectAccountId(enrollment.account_id),
            reply_artifact,
            "<closed-loop-reply@example.test>",
            NOW,
            outbound_message_id=outbound_id,
        )
        assert (
            await conversations.ingest_inbound(
                tenant,
                None,
                ProspectAccountId(enrollment.account_id),
                reply_artifact,
                "<closed-loop-reply@example.test>",
                NOW,
                outbound_message_id=outbound_id,
            )
            == message_id
        )

        organization = OrganizationServiceImpl(
            lambda bound: SqlAlchemyOrganizationUnitOfWork(factory, bound),
            Phase1OrganizationAuthorizer(tenant),
            now=lambda: NOW,
        )
        organization_boss = OrganizationActor(
            str(boss),
            OrganizationScope(OrganizationScopeLevel.TENANT, tenant),
            "boss",
        )
        organization_system = OrganizationActor(
            "system:closed-loop-playbook",
            OrganizationScope(OrganizationScopeLevel.SYSTEM, tenant),
            "system",
        )
        proposed = await organization.propose_playbook(
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
            actor=organization_boss,
            idempotency_key=f"closed-loop-playbook:{tenant}",
        )
        version = await organization.get_version(
            tenant, proposed.playbook_version_id, actor=organization_boss
        )
        await organization.activate_playbook(
            tenant,
            version.playbook_version_id,
            PlaybookApprovalFact(
                approval_id=ApprovalId(new_id("apr")),
                approval_type="playbook_change",
                change_set_ref=version.change_set_ref,
                decided_by=boss,
                decided_at=NOW,
            ),
            actor=organization_system,
        )

        opportunity_actor = OpportunityActor(
            str(boss),
            OpportunityScope(level=OpportunityScopeLevel.TENANT),
            "boss",
        )
        opportunities = OpportunityServiceImpl(
            lambda: SqlAlchemyOpportunityUnitOfWork(factory, tenant),
            OpportunityScorerImpl(
                ScoringPolicy(
                    version="closed-loop-v1",
                    value_band_boundaries=(
                        Money(Decimal(1000), CurrencyCode("USD")),
                        Money(Decimal(5000), CurrencyCode("USD")),
                    ),
                    bucket_map={rank: "high" for rank in range(1, 8)},
                )
            ),
            __import__(
                "domains.opportunities.models", fromlist=["HandoffPolicy"]
            ).HandoffPolicy(sla_seconds=3600, backlog_threshold=10),
            authorizer=Phase1OpportunityAuthorizer(tenant),
            audit=OpportunityAuditLogger(),
            now=lambda: NOW,
        )
        evidence_reader = ConversationReplyEvidenceReader(
            lambda bound: SqlAlchemyConversationsUnitOfWork(
                factory, bound, now=lambda: NOW
            )
        )
        business_reader = TenantBoundReplyBusinessFactsReader(
            tenant_id=tenant,
            outreach=outreach,
            demand=demand,
            prospecting=prospecting,
            opportunities=opportunities,
            opportunity_actor=opportunity_actor,
        )
        exact_context = ReplyActionContext(
            message_id=message_id,
            outbound_message_id=outbound_id,
            enrollment_id=enrollment.enrollment_id,
            account_id=enrollment.account_id,
            contact_point_id=contact_point_id,
        )
        pre_promotion_facts = await business_reader.load(tenant, exact_context)
        assert pre_promotion_facts is not None
        assert pre_promotion_facts.hypothesis_id == hypothesis_id
        assert pre_promotion_facts.need_id is None
        content = ArtifactMessageContentReader(
            lambda bound: SqlAlchemyConversationsUnitOfWork(
                factory, bound, now=lambda: NOW
            ),
            raw_store,
            max_raw_bytes=100_000,
            max_subject_chars=500,
            max_body_chars=10_000,
        )
        action_ports = ComposedReplyActionPorts(
            tenant_id=tenant,
            evidence=evidence_reader,
            business=business_reader,
            content=content,
            demand=demand,
            opportunities=opportunities,
            outreach=outreach,
            sending_identities=_UnusedSendingIdentities(),
            conversations=conversations,
            opportunity_intake=DurableReplyOpportunityIntake(
                tenant_id=tenant,
                demand=demand,
                prospecting=prospecting,
                organization=organization,
                opportunities=opportunities,
                employees=employees,
                evidence_reader=evidence_reader,
                organization_actor=organization_boss,
                opportunity_actor=opportunity_actor,
                employee_actor=EmployeeActor(str(boss), EmployeeScope.TENANT, "boss"),
            ),
        )
        reply_model = _ControlledReplyModel()
        reply_handlers = build_reply_qualification_handlers(
            classifier=QualificationAgent(
                model="controlled-reply-model",
                model_client=reply_model,
                gateway=None,
                guardrails=None,
            ),
            content_reader=content,
            input_guard=CredentialMarkerGuard(),
            conversations=conversations,
            outreach=outreach,
            tenant_id=tenant,
            now=lambda: NOW,
            action_ports=action_ports,
        )
        reply_engine = PostgresWorkflowEngine(factory, reply_handlers, now=lambda: NOW)
        reply_engine.register(build_reply_qualification_definition())
        reply_context = {
            "message_id": str(message_id),
            "outbound_message_id": str(outbound_id),
            "category": None,
            "enrollment_id": str(enrollment.enrollment_id),
            "account_id": str(enrollment.account_id),
            "contact_point_id": str(contact_point_id),
        }
        reply_run_id = await reply_engine.start(
            tenant,
            "reply_qualification",
            str(message_id),
            reply_context,
            f"reply:{message_id}",
        )
        await _poll_until_idle(reply_engine, tenant)
        await employee_session.commit()

        repeated_run_id = await reply_engine.start(
            tenant,
            "reply_qualification",
            str(message_id),
            reply_context,
            f"reply:{message_id}",
        )
        await _poll_until_idle(reply_engine, tenant)
        assert repeated_run_id == reply_run_id
        checked_run = await reply_engine.get_run(tenant, reply_run_id)
        assert reply_model.calls == 1, (checked_run.status, checked_run.last_error)

        async with factory() as session:
            reply_run = await session.get(WorkflowRunRow, reply_run_id)
            enrollment_row = await session.get(
                OutreachEnrollmentRow,
                (str(tenant), str(enrollment.enrollment_id)),
            )
            hypotheses = (
                (
                    await session.execute(
                        select(NeedHypothesisRow).where(
                            NeedHypothesisRow.tenant_id == tenant
                        )
                    )
                )
                .scalars()
                .all()
            )
            needs = (
                (
                    await session.execute(
                        select(ValidatedNeedRow).where(
                            ValidatedNeedRow.tenant_id == tenant
                        )
                    )
                )
                .scalars()
                .all()
            )
            opportunity_rows = (
                (
                    await session.execute(
                        select(OpportunityRow).where(OpportunityRow.tenant_id == tenant)
                    )
                )
                .scalars()
                .all()
            )
            handoffs = (
                (
                    await session.execute(
                        select(HandoffRow).where(HandoffRow.tenant_id == tenant)
                    )
                )
                .scalars()
                .all()
            )
            outbox_payloads = (
                (
                    await session.execute(
                        select(OutboxEventRow.event_payload).where(
                            OutboxEventRow.tenant_id == tenant
                        )
                    )
                )
                .scalars()
                .all()
            )
            directive_proposals = (
                (
                    await session.execute(
                        select(DirectiveProposalRow).where(
                            DirectiveProposalRow.tenant_id == tenant,
                            DirectiveProposalRow.proposal_id == proposal_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            persisted_reply_artifact = await session.get(
                RawArtifactRow, (str(tenant), reply_artifact)
            )

        assert reply_run.status == StepStatus.COMPLETED.value, reply_run.last_error
        assert enrollment_row.state == "replied"
        assert (
            len(hypotheses) == len(needs) == len(opportunity_rows) == len(handoffs) == 1
        )
        assert hypotheses[0].validated_need_id == needs[0].need_id
        assert opportunity_rows[0].need_id == needs[0].need_id
        assert opportunity_rows[0].owner == owner
        assert handoffs[0].opportunity_id == opportunity_rows[0].opportunity_id
        assert handoffs[0].state == "requested"
        assert len(directive_proposals) == 1
        assert directive_proposals[0].state == "confirmed"
        assert persisted_reply_artifact is not None
        assert persisted_reply_artifact.kind == "email_raw"

        opportunity = await opportunities.get_by_need(
            tenant, needs[0].need_id, actor=opportunity_actor
        )
        assert opportunity is not None
        assert opportunity.state == "assigned"
        assert opportunity.target_price == Money(Decimal(2), CurrencyCode("USD"))
        provenance = {item.field_name: item for item in opportunity.provenance}
        assert provenance["account_name"].page_hash == PAGE_HASH
        assert provenance["quantity"].source_id == message_id
        assert provenance["target_price"].source_id == message_id

        pending = await opportunities.list_pending_handoffs(
            tenant, opportunity_actor, limit=10
        )
        assert len(pending) == 1
        assert pending[0].opportunity_id == opportunity.opportunity_id
        assert pending[0].customer_verbatim == BODY
        assert pending[0].evidence_links == [reply_artifact]
        assert pending[0].assigned_to == owner

        facts = await business_reader.load(tenant, exact_context)
        assert facts is not None
        assert facts.hypothesis_id == hypothesis_id
        assert facts.need_id == needs[0].need_id
        assert facts.opportunity_id == opportunity.opportunity_id
        assert await evidence_reader.load(other_tenant, message_id) is None
        with pytest.raises(TenantIsolationViolation):
            await business_reader.load(other_tenant, exact_context)
        with pytest.raises(ValidationError, match="Enrollment 关联不匹配"):
            await business_reader.load(
                tenant,
                ReplyActionContext(
                    message_id=message_id,
                    outbound_message_id=outbound_id,
                    enrollment_id=enrollment.enrollment_id,
                    account_id=ProspectAccountId(new_id("acc")),
                    contact_point_id=contact_point_id,
                ),
            )

        persisted_control_plane = json.dumps(
            {
                "discovery_context": discovery_run.context,
                "reply_context": reply_run.context,
                "outbox": outbox_payloads,
            },
            default=str,
        )
        assert BODY not in persisted_control_plane
        assert EMAIL not in persisted_control_plane
        assert "api_key=" not in persisted_control_plane
        assert all(BODY not in record.getMessage() for record in caplog.records)
        assert all(EMAIL not in record.getMessage() for record in caplog.records)
        assert (
            await employee_session.scalar(
                select(func.count())
                .select_from(OpportunityRow)
                .where(OpportunityRow.tenant_id == tenant)
            )
            == 1
        )
    finally:
        await employee_session.close()
        await engine.dispose()
