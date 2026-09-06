"""T10公开服务前置；仅员工身份bootstrap，业务状态不直接写表。

联系人/DNS/历史投递为显式受控观察，不调用外部验证或发送。报价生产工厂、
真实客户回复verifier、PG仓储、Gateway和Linux解析器均保留。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api import runtime as api
from apps.api.composition.demand_radar import ProspectingDemandAccountNames
from apps.api.composition.runtime import _ServiceBackedCampaignApprovalProvider
from apps.api.dependencies import get_api_dependencies
from apps.composition_support.outreach_fact_readers import CurrentReplyStatusReader
from apps.scheduler_worker import runtime as worker
from apps.scheduler_worker.adapters.reply_customer_evidence import (
    TenantBoundCustomerReplyEvidenceVerifier,
)
from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
from artifact_store.store import RawArtifactKind
from connectors.object_store import s3
from domains.approvals.service import ApprovalType, BlastRadius
from domains.conversations.schemas import ReplyCategory, ReplyFieldEvidence
from domains.demand.schemas import CustomerReplyEvidenceClaim, SignalCaptureRequest
from domains.demand.service_impl import DemandServiceImpl
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope, ScopeLevel
from domains.opportunities.schemas import (
    OpportunityCreateRequest,
    ValidatedNeedEvidence,
)
from domains.outreach import schemas as outreach_schema
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import (
    OutreachScope,
    Phase1OutreachAuthorizer,
    StandardAuditLogger,
)
from domains.outreach.permissions import ScopeLevel as OutreachLevel
from domains.outreach.service_impl import OutreachServiceImpl
from domains.prospecting import schemas as prospecting_schema
from domains.sending_identity.permissions import Actor as SenderActor
from domains.sending_identity.permissions import ScopeLevel as SenderLevel
from domains.sending_identity.permissions import SendingIdentityScope
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DomainRole,
    IdentityRegisterRequest,
)
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.tables import EmployeeRow
from shared.errors import ValidationError
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import EmployeeId, OutboundMessageId, TenantId, new_id
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType
from tests.integration.test_api_runtime import _runtime_env
from tests.integration.test_quote_runtime import (
    ControlledObjects,
    runtime_request,
    worker_environment,
)
from tests.integration.test_scheduler_worker import (
    _factory_dependencies,
    _FactoryHealthServer,
    _FactoryResolver,
)
from tests.quotation_runtime_fixtures import quotation_settings_values
from tests.unit.test_evidence_text_profiles import pdf_bytes
from tool_gateway.pipeline import ToolGateway
from workflows.employee_work_intake.schemas import WorkSourceKind

NOW = datetime(2026, 8, 29, 8, tzinfo=UTC)
BODY = "We need 50 pieces of hardware, steel, 50 mm, cartons, destination DE, target USD 4.00."
BOSS = EmployeeId("emp_" + "0" * 25 + "1")
PRODUCT = EmployeeId("emp_" + "0" * 25 + "2")
DECIDER = EmployeeId("emp_" + "0" * 25 + "3")
OWNER = EmployeeId("emp_" + "0" * 25 + "4")


class HistoricalEligibility:
    """资格仅投影公开持久服务，不制造Sendable/Verified票据。"""

    def __init__(self, case):
        self.case = case

    async def get_contact_eligibility(self, tenant, contact_point, account):
        detail = await self.case.dependencies.prospecting.get_account_detail(tenant, account)
        point = next(
            point for contact in detail.contacts for point in contact.contact_points
            if point.contact_point.contact_point_id == contact_point
        )
        hypothesis = await self.case.demand.get_hypothesis(tenant, self.case.hypothesis)
        if hypothesis.account_id != account or hypothesis.status not in {"inferred", "contacting"}:
            raise ValidationError("历史前置假设不属于当前企业或不再允许触达")
        return outreach_schema.ContactEligibilitySnapshot(
            tenant, contact_point, account,
            outreach_schema.ContactVerificationStatus(point.contact_point.verification.value),
            point.contact_point.verified_at,
            outreach_schema.ContactLegalBasis(point.legal_basis.value), point.assessment_ref,
            True, detail.account.country, detail.account.entity_type,
            frozenset({hypothesis.category}), NOW,
        )

    async def get_sending_identity_eligibility(self, tenant, identity):
        actor = SenderActor(str(BOSS), SendingIdentityScope(SenderLevel.TENANT), "boss")
        value = await self.case.dependencies.sending_identities.get(tenant, identity, actor=actor)
        permission = await self.case.dependencies.sending_identities.check_send_permission(
            tenant, identity, True, actor=actor,
        )
        return outreach_schema.SendingIdentityEligibilitySnapshot(
            tenant, identity, outreach_schema.OutreachSenderRole(value.role.value),
            value.auth is not None and value.auth.all_passed,
            permission.allowed, permission.remaining_today, NOW,
        )

    async def get_campaign_approval(self, tenant, campaign, version):
        raise AssertionError("受控Campaign必须已有真实审批，不得回退")

    async def get_reply_status(self, tenant, contact_point, account):
        reader = CurrentReplyStatusReader(
            self.case.tenant, self.case.dependencies.prospecting,
            self.case.dependencies.conversations, now=lambda: NOW,
        )
        return await reader.get_reply_status(tenant, contact_point, account)


async def initialize_public_case(case):
    """建立受控历史链；生产服务承担所有业务状态转换。"""
    tenant, deps = case.tenant, case.dependencies
    case.actor, case.actor_id, case.file_actor_id = PRODUCT, PRODUCT, BOSS
    case.decider, case.owner = DECIDER, OWNER
    async with case.sessions.begin() as session:
        for employee, role in ((BOSS, "boss"), (PRODUCT, "product"), (DECIDER, "boss"), (OWNER, "sales")):
            session.add(EmployeeRow(
                tenant_id=tenant, employee_id=employee, name=employee,
                role=role, is_active=True, created_at=NOW, user_id=None, team_id=None,
                manager_id=None, languages=[], timezone="UTC", max_active_accounts=None,
            ))
    raw = deps.work_uploads._artifacts
    email = await raw.put(
        tenant, RawArtifactKind.EMAIL_RAW,
        ("Content-Type: text/plain; charset=utf-8\r\n\r\n" + BODY + "\r\n").encode(),
        "message/rfc822",
    )
    case.raw_artifact = email
    provenance = Provenance(SourceType.EMPLOYEE_INPUT, email.artifact_id, "human", NOW, BOSS, NOW)
    account = await deps.prospecting.resolve_account(tenant, prospecting_schema.AccountResolveRequest(
        "Controlled Buyer", "DE", entity_type="importer",
        field_provenance={"name": provenance, "country": provenance},
    ))
    contact = await deps.prospecting.create_contact(tenant, prospecting_schema.ContactCreateRequest(account))
    point = await deps.prospecting.add_contact_point(tenant, prospecting_schema.ContactPointCreateRequest(
        contact, prospecting_schema.ContactPointKind.EMAIL, "procurement@example.test",
        prospecting_schema.LegalBasisInput(
            prospecting_schema.LegalBasisType.LEGITIMATE_INTEREST,
            prospecting_schema.SubjectType.LEGAL_ENTITY, prospecting_schema.ContactType.ROLE_BASED,
            "controlled-history", NOW, assessment_ref="controlled-assessment",
        ),
    ))
    await deps.prospecting.record_verification(tenant, prospecting_schema.VerificationRecordRequest(
        point, prospecting_schema.VerificationStatus.VERIFIED,
        "controlled-history", NOW, "受控观察，真实验证not_run",
    ))
    sender_actor = SenderActor(str(BOSS), SendingIdentityScope(SenderLevel.TENANT), "boss")
    sender = await deps.sending_identities.register(tenant, IdentityRegisterRequest(
        "sales@controlled.example.test", "controlled.example.test", DomainRole.COLD_OUTREACH,
    ), actor=sender_actor)
    await deps.sending_identities.begin_authentication(tenant, sender, actor=sender_actor)
    await deps.sending_identities.record_authentication_result(
        tenant, sender, AuthenticationResult(NOW, True, True, True, (), "controlled-dns-history"),
        actor=SenderActor("system:t10-history", SendingIdentityScope(
            SenderLevel.SYSTEM, allowed_identity_ids=frozenset({sender}),
        ), "system"),
    )
    await deps.sending_identities.start_warmup(tenant, sender, 10, actor=sender_actor)
    eligibility = HistoricalEligibility(case)
    outreach = OutreachServiceImpl(
        lambda bound: SqlAlchemyOutreachUnitOfWork(case.sessions, bound, now=lambda: NOW),
        eligibility, eligibility,
        _ServiceBackedCampaignApprovalProvider(deps.approvals, eligibility), eligibility,
        Phase1OutreachAuthorizer(tenant), StandardAuditLogger(), now=lambda: NOW,
    )
    demand = DemandServiceImpl(
        lambda bound: SqlAlchemyDemandUnitOfWork(case.sessions, bound, now=lambda: NOW),
        now=lambda: NOW,
        account_names=ProspectingDemandAccountNames(deps.prospecting),
        customer_evidence=TenantBoundCustomerReplyEvidenceVerifier(
            tenant_id=tenant,
            conversations_uow_factory=lambda bound: SqlAlchemyConversationsUnitOfWork(
                case.sessions, bound, now=lambda: NOW,
            ), outreach=outreach,
        ),
    )
    signal = await demand.capture_signal(tenant, SignalCaptureRequest(
        "product_line_expansion", "Controlled Buyer", "受控历史企业扩展观察",
        NOW, "employee_input", email.artifact_id, "human", possible_need="hardware",
    ))
    hypothesis = await demand.create_hypothesis(
        tenant, account, "hardware", [signal], "需要客户回复验证的受控假设", "human",
    )
    case.demand, case.hypothesis = demand, hypothesis
    campaign_actor = OutreachActor(str(BOSS), OutreachScope(OutreachLevel.TENANT), "boss")
    campaign = await outreach.create_campaign(tenant, outreach_schema.CampaignCreateRequest(
        "Controlled history only", ("DE",), ("importer",), ("hardware",), (sender,),
        (outreach_schema.SequenceStepRequest(1, outreach_schema.StepIntent.DISCOVERY, 0),),
        1, 1, (),
    ), actor=campaign_actor)
    await outreach.submit_campaign(tenant, campaign.campaign_id, actor=campaign_actor)
    approval = await deps.approvals.submit(
        tenant, ApprovalType.CAMPAIGN_BOUNDARY_CHANGE, "受控历史Campaign",
        {"campaign_id": campaign.campaign_id, "version": 1}, "仅测试历史前置，不外发",
        BlastRadius([str(campaign.campaign_id)], "允许受控历史入组", "不建立历史入组", True),
        proposed_by_employee=BOSS, change_set_ref=f"campaign:{campaign.campaign_id}:v1",
    )
    await deps.approvals.decide(tenant, approval, True, DECIDER)
    await outreach.activate_campaign(tenant, campaign.campaign_id, actor=campaign_actor)
    enrollment = await outreach.enroll(tenant, campaign.campaign_id,
        outreach_schema.EnrollmentCreateRequest(account, point, "t10-history-enrollment", hypothesis),
        actor=campaign_actor,
    )
    send_actor = OutreachActor("system:t10-history", OutreachScope(
        OutreachLevel.SYSTEM, allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
    ), "system")
    attempt = await outreach.prepare_message_attempt(tenant, enrollment.enrollment_id, actor=send_actor)
    bound = await outreach.bind_delivery_correlation(
        tenant, attempt.attempt_id,
        outreach_schema.DeliveryCorrelationBinding(
            "<t10-history." + "d" * 64 + "@messages.tradeos.invalid>",
            "t10-history." + "d" * 64, "t10-history",
        ),
        actor=OutreachActor("system:t10-history", OutreachScope(
            OutreachLevel.SYSTEM, allowed_attempt_ids=frozenset({attempt.attempt_id}),
        ), "system"),
    )
    await outreach.claim_message_send(tenant, attempt.attempt_id, actor=send_actor)
    outbound = OutboundMessageId(bound.deterministic_message_id)
    message = await deps.conversations.ingest_inbound(
        tenant, None, account, email.artifact_id, "<t10-reply@example.test>", NOW,
        outbound_message_id=outbound,
    )
    values = {"product_category": "hardware", "quantity": "50", "material": "steel",
              "size_spec": "50 mm", "packaging": "cartons", "destination": "DE",
              "target_price": {"amount": "4.00", "currency": "USD"}}
    await deps.conversations.record_classification(
        tenant, message, ReplyCategory.PROVIDES_SPECIFICATION, "human:controlled-history",
        outbound_message_id=outbound,
        candidate_fields=tuple(ReplyFieldEvidence(name, str(value), BODY) for name, value in values.items()
                               if name != "target_price"),
    )
    claim = CustomerReplyEvidenceClaim(
        hypothesis, message, outbound, enrollment.enrollment_id, account, point,
    )
    with pytest.raises(ValidationError):
        await demand.record_customer_reply_evidence(tenant, claim)
    await outreach.record_sent(tenant, attempt.attempt_id, "controlled-history-no-transport", actor=send_actor)
    await demand.record_customer_reply_evidence(tenant, claim)
    case.need = await demand.promote_to_validated(
        tenant, hypothesis, message,
        {name: {"value": value, "quote": BODY, "extracted_by": "human"} for name, value in values.items()},
        confirmed_by=BOSS,
    )
    case.demand, case.account, case.message = demand, account, message
    reply_provenance = Provenance(SourceType.CONVERSATION, message, "human", NOW, BOSS, NOW, source_quote=BODY)
    opportunity_actor = OpportunityActor(str(BOSS), OpportunityScope(ScopeLevel.TENANT), "boss")
    need = await demand.get_need(tenant, case.need)
    case.opportunity = await deps.opportunities.create_from_need(tenant, OpportunityCreateRequest(
        str(case.need), str(account), "Controlled Buyer", "DE", need.product_category,
        "customer_specification", bool(await deps.prospecting.list_verified_contact_points(tenant, account)),
        True, Money(Decimal("1.00"), CurrencyCode("USD")),
        field_provenance={"account_name": provenance, "country": provenance,
                          "quantity": reply_provenance, "destination": reply_provenance,
                          "target_price": reply_provenance},
        quantity=need.quantity, destination=need.destination, target_price=need.target_price,
        estimated_order_value=need.target_price.multiply(need.quantity),
    ), ValidatedNeedEvidence(EvidenceLevel.CUSTOMER_SPECIFICATION, reply_provenance), actor=opportunity_actor)
    assert case.opportunity is not None
    async with deps.employees(tenant) as employees:
        ownership = await employees.resolve_owner(
            tenant, account, actor=EmployeeActor(str(BOSS), EmployeeScope.TENANT, "boss"),
            country="DE", need_category="hardware", boss_override=OWNER,
        )
    await deps.opportunities.assign(tenant, case.opportunity, ownership.owner, BOSS, actor=opportunity_actor)
    case.statement = "Quoted unit price: USD 2.00 for 50 pieces."
    upload = await deps.work_uploads.create_upload(
        tenant, PRODUCT, uploaded_by=None, artifact_kind=RawArtifactKind.PDF,
        source_kind=WorkSourceKind.PDF_TEXT, content=pdf_bytes(case.statement),
        mime_type="application/pdf", occurred_at=NOW, customer_timezone="UTC",
    )
    case.source = "upload:" + upload.upload_id
    policy_upload = await deps.work_uploads.create_upload(
        tenant, BOSS, uploaded_by=None, artifact_kind=RawArtifactKind.PDF,
        source_kind=WorkSourceKind.PDF_TEXT,
        content=pdf_bytes("Controlled policy: minimum 0.10; target 0.20. All costs classified as goods."),
        mime_type="application/pdf", occurred_at=NOW, customer_timezone="UTC",
    )
    case.policy_source = "upload:" + policy_upload.upload_id
    return case


@asynccontextmanager
async def runtime_case(engine, monkeypatch, *, cors_origins=()):
    """保留实际API lifespan与独立worker工厂，SDK网络才是替身。"""
    tenant = TenantId(new_id("tn"))
    environ = _runtime_env(engine.url.render_as_string(False))
    environ.update(TRADEOS_TENANT_ID=tenant,
                   TRADEOS_QUOTATION_SETTINGS_JSON=json.dumps(quotation_settings_values()),
                   TRADEOS_CORS_ALLOWED_ORIGINS=json.dumps(list(cors_origins) or ["http://127.0.0.1:4173"]),
                   PYTHON_DOTENV_DISABLED="1")
    objects, calls = ControlledObjects(), Counter()
    monkeypatch.setattr(s3.boto3, "client", objects.client)
    monkeypatch.setattr(api.os, "environ", environ)

    class BusinessClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(api, "datetime", BusinessClock)
    invoke = ToolGateway.invoke

    async def counted(gateway, context):
        calls[context.tool_id] += 1
        return await invoke(gateway, context)

    monkeypatch.setattr(ToolGateway, "invoke", counted)
    app = api.create_runtime_app()
    deps = get_api_dependencies(Request({"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"}))
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app), base_url="http://test",
    ) as client:
        assert deps.quotation.evidence.parser.capability().status == "available"
        case = SimpleNamespace(
            app=app, client=client, dependencies=deps, tenant=tenant, clock=[NOW], objects=objects,
            sessions=async_sessionmaker(engine, expire_on_commit=False),
            gateway_calls=MappingProxyType(calls),
        )
        await initialize_public_case(case)
        worker_env = worker_environment(engine, tenant, "enabled")
        worker_env.update(TEST_ACCESS="controlled-test-access", TEST_SECRET="controlled-test-secret",
                          TRADEOS_SCHEDULER_INTERVAL_SECONDS="1")
        factory = worker.SchedulerRuntimeFactory(
            worker_env, _factory_dependencies(worker, with_hunter=False),
            resolver_factory=_FactoryResolver, health_server_factory=_FactoryHealthServer, now=lambda: NOW,
        )
        async with factory() as runtime:
            assert runtime.activation.quotation_lifecycle._parser is not deps.quotation.evidence.parser
            case.worker = runtime
            stop = asyncio.Event()
            task = asyncio.create_task(run_scheduler_worker(runtime, stop_event=stop, install_signal_handlers=False))
            case.worker_task = task
            try:
                yield case
            finally:
                stop.set()
                result = await asyncio.wait_for(task, timeout=10)
                assert result.status is WorkerStartStatus.STARTED
                assert result.cycles_completed > 0
                assert all(calls.get(name, 0) == 0 for name in (
                    "email.send", "contact.enrich", "contact.verify", "web.search"))
                print(f"t10_worker_started_cycles={result.cycles_completed}", flush=True)
                print("t10_forbidden_gateway_calls=0", flush=True)


async def wait_until(read, predicate, *, label):
    """有界状态轮询只等待真实engine效果，不直接推进handler。"""
    async with asyncio.timeout(20):
        while True:
            value = await read()
            if predicate(value):
                return value
            await asyncio.sleep(0.05)


async def locate(case, source, scope, profile, page, excerpt, *, actor):
    """原件文本/hash/定位来自同一真实受限parser。"""
    preview = await runtime_request(case, "POST", "/evidence/preview", {
        "operation": "preview", "source_ref": source, "scope": scope,
        "profile": profile, "page": page,
    }, actor=actor)
    start = preview["text"].index(excerpt)
    return await runtime_request(case, "POST", "/evidence/locator", {
        "operation": "locate", "source_ref": source, "scope": scope, "profile": profile,
        "page": page, "start": start, "end": start + len(excerpt),
        "expected_raw_hash": preview["raw_hash"], "expected_text_hash": preview["text_hash"],
    }, actor=actor)


async def prepare_customer_facts(case):
    """客户原文boss-only；成本员工不能代做客户单位确认。"""
    unit = await runtime_request(case, "GET", f"/needs/{case.need}/unit", actor=BOSS)
    located = await locate(case, "message:" + case.message,
        {"purpose": "need_unit", "need_id": case.need, "action": "confirm"},
        "rfc822-plain-v1", None, "50 pieces", actor=BOSS,
    )
    await runtime_request(case, "POST", f"/needs/{case.need}/unit-confirmations", {
        "unit": "pieces", "source_message_id": case.message, "locator": located["locator"],
        "source_quote": "50 pieces", "expected_quantity_fact_hash": unit["quantity_fact_hash"],
        "expected_unit_confirmation_id": None,
    }, actor=BOSS, key="t10-unit")
    await runtime_request(case, "POST", "/issuer", {
        "name": "Controlled Supplier", "address": "Test address",
        "contact": "sales@example.test",
    }, actor=BOSS, key="t10-issuer")


async def prepare_quote_inputs(case):
    """显式测试政策/单件成本/22项清单；只准备，不提前创建报价。"""
    from domains.costing.service import cost_item_type_values
    from domains.quotations.schemas import QuoteDraftCommand

    await prepare_customer_facts(case)
    context = await runtime_request(case, "GET", f"/opportunities/{case.opportunity}/quote-context")
    assert context["blockers"] == []
    located = await locate(case, case.source, {"purpose": "pricing"}, "pdf-text-v1", 1,
                           case.statement, actor=PRODUCT)
    valid_until = (NOW + timedelta(hours=12)).isoformat()
    price = await runtime_request(case, "POST", "/price-evidence", {
        "kind": "supplier_price", "opportunity_id": case.opportunity, "need_id": case.need,
        "supplier_ref": "controlled:supplier", "specification": "hardware steel 50 mm cartons",
        "unit": "pieces", "destination": "DE", "currency": "USD", "source_ref": case.source,
        "locator": located["locator"], "amount": "2.00", "basis": "quoted",
        "quantity_min": 50, "quantity_max": 50, "moq": 1,
        "quoted_at": NOW.isoformat(), "valid_until": valid_until,
    }, key="t10-price")
    policy = await runtime_request(case, "POST", "/policies", {
        "category": None, "minimum_margin_rate": "0.10", "target_margin_rate": "0.20",
        "cost_groups": {name: "goods" for name in cost_item_type_values()},
        "effective_from": NOW.isoformat(), "source_ref": case.policy_source,
    }, actor=BOSS, key="t10-policy")
    sheet = await runtime_request(case, "POST", f"/opportunities/{case.opportunity}/cost-sheets", {
        "version_type": "quoted", "quantity": 50, "base_currency": "USD", "quote_currency": "USD",
        "fx_snapshot_id": "controlled-cost-fx", "fx_rates": [],
    }, expected=201)
    sheet_id = sheet["cost_sheet_id"]
    await runtime_request(case, "POST", f"/cost-sheets/{sheet_id}/items", {
        "item_type": "product_purchase", "amount": "2.00", "currency": "USD", "price_basis": "quoted",
        "is_per_unit": True, "source_ref": price["evidence_id"], "note": None,
    }, expected=204)
    sheet = await runtime_request(case, "GET", f"/cost-sheets/{sheet_id}")
    coverage = await runtime_request(case, "POST", f"/cost-sheets/{sheet_id}/coverage", {
        "expected_sheet_hash": sheet["content_hash"], "acquisition_mode": "detail",
        "decisions": [{
            "item_type": name, "applicable": name == "product_purchase", "reason": "受控范围逐项核对",
            "item_bindings": [{"item_sequence": 1, "evidence_id": price["evidence_id"],
                               "source_line_ref": located["locator"], "allocation_scope": "order:t10"}]
            if name == "product_purchase" else [],
        } for name in cost_item_type_values()],
    }, key="t10-coverage")
    scope = await runtime_request(case, "POST", f"/cost-sheets/{sheet_id}/scope-confirmations", {
        "coverage_id": coverage["coverage_id"], "expected_sheet_hash": sheet["content_hash"],
        "expected_coverage_hash": coverage["content_hash"], "expected_need_facts_hash": context["need_facts_hash"],
        "terms": [], "valid_until": valid_until,
        "evidence_bindings": [{"evidence_id": price["evidence_id"], "evidence_hash": price["evidence_hash"],
                               "applicability_note": "人工核对规格包装目的地数量适用"}],
    }, key="t10-scope")
    case.command = QuoteDraftCommand.model_validate_json(json.dumps({
        "opportunity_id": case.opportunity, "cost_sheet_id": sheet_id,
        "expected_context_hash": context["context_hash"], "expected_sheet_hash": sheet["content_hash"],
        "scope_confirmation_id": scope["confirmation_id"],
        "unit_price": {"amount": "4.00", "currency": "USD"},
        "rounding": {"unit_places": 2, "total_places": 2, "strategy": "ROUND_HALF_UP"},
        "quote_fx_ref": None, "valid_until": valid_until, "terms": [],
        "replaces_quote_id": None, "expected_quote_version": None,
    }))
    composition = case.dependencies.quotation
    case.application, case.quotations = composition.domain.creation, composition.domain.quotations
    case.files, case.approvals = composition.files_application, case.dependencies.approvals
    case.context_provider = composition.domain.context_provider
    case.policy, case.price_evidence, case.coverage = policy, price, coverage

    async def submit_and_approve(quote_id):
        from domains.approvals.schemas import ApprovalReaderIdentity

        await composition.approval_starter.start(case.tenant, quote_id, actor_id=PRODUCT)

        async def read():
            return await runtime_request(case, "GET", f"/quotes/{quote_id}")

        async def packages():
            values = await case.approvals.list_for_reader(case.tenant,
                reader=ApprovalReaderIdentity(employee_id=DECIDER, role="boss"))
            return [value for value in values if value.proposed_change_display.get("报价编号") == quote_id]

        submitted = await wait_until(packages, lambda values: len(values) == 1, label="审批包")
        await case.approvals.decide(case.tenant, submitted[0].approval_id, True, DECIDER)
        await wait_until(read, lambda q: q["state"] == "approved", label="批准应用")

    case.submit_and_approve = submit_and_approve
