"""真实PG队列与域服务消费；外部证据/验证和当前政策事实使用合成端口，不发送。"""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from apps.composition_support.outreach_fact_readers import (
    CurrentContactEligibilityReader,
)
from apps.scheduler_worker.customer_profile_evidence import (
    ArchivedCustomerProfileEvidence,
)
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from connectors.web_search.client import PageSnapshot
from domains.compliance.schemas import CountryPolicyAction, CountryPolicyDecision
from domains.demand.service_impl import DemandServiceImpl
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import (
    EmployeeScope,
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from domains.employees.service_impl import EmployeeServiceImpl
from domains.outreach.customer_agent_policy import (
    CustomerAgentKnowledgeRef,
    CustomerAgentPolicyInput,
)
from domains.outreach.permissions import (
    Actor,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignCreateRequest,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    StepIntent,
)
from domains.outreach.service_impl import OutreachServiceImpl
from domains.prospecting.agent_profile import AgentPreparationObservation
from domains.prospecting.inventory import InventoryFollowupScope, InventoryQuery
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.customer_preparation import (
    PostgresAgentPreparationTransactions,
    _PreparationAccountNames,
)
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.prospect_inventory_tables import CustomerPreparationItemRow
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.tables import (
    ContactPointRow,
    DemandSignalRow,
    EmployeeRow,
    NeedHypothesisRow,
    OutreachEnrollmentRow,
)
from shared.schemas.identifiers import ApprovalId, SendingIdentityId, new_id
from tests.integration.test_agent_profile_evidence import Objects
from tests.integration.test_customer_assignment_bridge import NOW
from tests.integration.test_customer_preparation import TestHasher
from tests.outreach_fakes import FakeApprovals, FakeAudit, FakeSenders, Trace
from tests.unit.test_prospect_inventory import candidate
from workflows.customer_preparation.authority import CustomerAgentScopeAuthority
from workflows.customer_preparation.ports import ExtractedPreparation
from workflows.customer_preparation.service import CustomerPreparationConsumer

pytest_plugins = ("tests.integration.test_customer_assignment_bridge",)


async def setup_consumer(bridge_inventory, failure=None, *, multiple_senders=False):
    queue, catalog, factory, tenant, boss, sales, _ = bridge_inventory
    user = new_id("usr")
    async with factory() as session, session.begin():
        await session.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == boss).values(user_id=user))
    @asynccontextmanager
    async def employees(t):
        async with factory() as session, session.begin():
            repo = EmployeeRepositoryImpl(session, t)
            yield EmployeeServiceImpl(employees=repo, ownership=OwnershipRepositoryImpl(session, t), territories=TerritoryRepositoryImpl(session, t),
                now=lambda: NOW, manager_pool=lambda _: (), count_active_accounts=repo.count_active_accounts,
                authorizer=Phase1EmployeeAuthorizer(t), audit=StandardAuditLogger())
    prospecting = ProspectingServiceImpl(lambda t: SqlAlchemyProspectingUnitOfWork(factory, t), TestHasher(), now=lambda: NOW)
    demand = DemandServiceImpl(lambda t: SqlAlchemyDemandUnitOfWork(factory, t, now=lambda: NOW), now=lambda: NOW, account_names=_PreparationAccountNames(prospecting))
    trace, sender_id = Trace(), SendingIdentityId(new_id("sid"))
    sender_ids = tuple(sorted((sender_id, SendingIdentityId(new_id("sid"))))) if multiple_senders else (sender_id,)
    senders = FakeSenders({identity: SendingIdentityEligibilitySnapshot(tenant, identity, OutreachSenderRole.COLD_OUTREACH, True, True, 10, NOW) for identity in sender_ids}, trace)
    approvals = FakeApprovals(trace)
    class Replies:
        async def get_reply_status(self, t, point, account):
            return ReplyStatusSnapshot(t, point, account, ReplyState.NO_REPLY, None, NOW)
    outreach = OutreachServiceImpl(lambda t: SqlAlchemyOutreachUnitOfWork(factory, t, now=lambda: NOW),
        CurrentContactEligibilityReader(tenant, prospecting, demand, now=lambda: NOW), senders, approvals, Replies(), Phase1OutreachAuthorizer(tenant), FakeAudit(trace), now=lambda: NOW)
    actor = Actor(str(boss), OutreachScope(level=ScopeLevel.TENANT), "boss")
    campaign = await outreach.create_campaign(tenant, CampaignCreateRequest(name="合成准备", markets=("US",), target_entity_types=("distributor",),
        allowed_categories=("hinges",), sender_identity_ids=sender_ids, steps=(SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
        daily_new_contact_limit=5, daily_total_message_limit=5, handoff_triggers=(),
        customer_agent_policy=CustomerAgentPolicyInput(owner_employee_id=sales, assessment_ref="lia:explicit-test",
            confirmed_refs=(CustomerAgentKnowledgeRef(document_id="doc-test", revision_id="rev-test"),))), actor=actor)
    await outreach.submit_campaign(tenant, campaign.campaign_id, actor=actor)
    approval = CampaignApprovalSnapshot(tenant, campaign.campaign_id, 1, ApprovalId(new_id("apr")), CampaignApprovalState.APPROVED, boss, NOW)
    approvals.values[(campaign.campaign_id, 1)] = approval
    await outreach.activate_campaign(tenant, campaign.campaign_id, actor=actor)
    class Knowledge:
        async def get_detail(self, t, actor, doc):
            return SimpleNamespace(document=SimpleNamespace(tenant_id=t, document_id=doc, status="confirmed", current_revision_id="rev-test", source=SimpleNamespace(artifact_id="art-test", sha256="a" * 64)),
                revision=SimpleNamespace(document_id=doc, revision_id="rev-test", confirmed_by=boss, confirmed_at=NOW, analysis=SimpleNamespace(facts=[], inferences=[]), fact_provenance=[]))
    authority = CustomerAgentScopeAuthority(employees=employees, lookup_actor=EmployeeActor("system:test", EmployeeScope.SYSTEM, "system"), outreach=outreach, approvals=approvals, knowledge=Knowledge())
    queue.followup_authority = authority
    await catalog.save_public((candidate(),))
    allocation = (await queue.request(tenant, boss, InventoryQuery(country="US", product_tags=("hinges",)), 1, "auto", followup_scope=InventoryFollowupScope(campaign_id=campaign.campaign_id, campaign_version=1))).allocations[0]
    if failure == "withdrawn":
        approvals.values[(campaign.campaign_id, 1)] = replace(approval, state=CampaignApprovalState.REJECTED, approved_by=None, approved_at=None)
    if failure == "sender":
        senders.snapshots[sender_id] = replace(senders.snapshots[sender_id], sendable=False)
    objects = Objects()
    store = RawArtifactStoreImpl(lambda t: SqlAlchemyArtifactUnitOfWork(factory, t), objects, 2097152, lambda: NOW, new_id, bounded_transport=objects)
    class Extractor:
        calls = 0
        async def extract(self, claim, allocation, context, guard, *, target_entity_types):
            self.calls += 1
            await guard()
            text = "Acme Ltd. We distribute hinges. Sales sales@acme.com."
            meta = await store.put(tenant, RawArtifactKind.WEB_SNAPSHOT, f"<p>{text}</p>".encode(), "text/html")
            page = PageSnapshot(text, "https://acme.com/about", NOW, meta.content_hash, meta.artifact_id)
            reader = ArchivedCustomerProfileEvidence(tenant, page, store)
            observation = AgentPreparationObservation(entity_type={"value": "distributor", "quote": "We distribute hinges"}, industry={"value": "hardware", "quote": "hinges"}, subject_type="legal_entity",
                legal_entity_quote="Acme Ltd", public_email="sales@acme.com", email_quote="Sales sales@acme.com", fit_quote="We distribute hinges", fit_reasoning="推断：适配铰链；不是已验证需求", matched=failure != "fit", category="hinges")
            return ExtractedPreparation(await reader.read(tenant, meta.artifact_id), reader, observation, "agent:codex/synthetic", NOW)
    class Verifier:
        calls = 0
        async def verify(self, t, point):
            self.calls += 1
            assert (await prospecting.get_contact_point(t, point)).verification.value == "unverified"
            if failure == "unknown":
                raise RuntimeError("synthetic unknown outcome")
            return EmailVerificationResult(EmailVerificationOutcome.INVALID if failure == "invalid" else EmailVerificationOutcome.VERIFIED, "synthetic", NOW, VerificationCostNote.COUNTED)
    class Compliance:
        async def get_country_policy_decision(self, t, country, action, *, actor):
            return CountryPolicyDecision(country_key="us", action=CountryPolicyAction.COLD_B2B_EMAIL, configured=True, allowed=failure != "policy", active_version_id=new_id("cpp"), content_hash="a" * 64,
                requirements=("manual_review",) if failure == "requirement" else ("retain.assessment_ref", "honor_opt_out"))
    extractor, verifier = Extractor(), Verifier()
    consumer = CustomerPreparationConsumer(tenant_id=tenant, queue=queue, authority=authority, employees=employees,
        prospecting=prospecting, outreach=outreach, compliance=Compliance(), senders=senders,
        transactions=PostgresAgentPreparationTransactions(factory, TestHasher(), now=lambda: NOW), extractor=extractor,
        verifier=None if failure == "missing_verifier" else verifier, now=lambda: NOW)
    return consumer, allocation, extractor, verifier, prospecting


@pytest.mark.parametrize("failure", [None, "missing_verifier", "sender", "withdrawn", "policy", "requirement", "fit", "invalid", "unknown"])
async def test_real_queue_to_recorded_verification_and_exact_enrollment(bridge_inventory, failure):
    consumer, _allocation, extractor, verifier, prospecting = await setup_consumer(bridge_inventory, failure)
    await consumer.scan()
    await consumer.scan()
    _, _, factory, tenant, _, _sales, _ = bridge_inventory
    async with factory() as session:
        item = (await session.scalars(select(CustomerPreparationItemRow).where(CustomerPreparationItemRow.tenant_id == tenant))).one()
        enrollments = (await session.scalars(select(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id == tenant))).all()
        count = await session.scalar(select(func.count()).select_from(ContactPointRow).where(ContactPointRow.tenant_id == tenant))
        if failure is None:
            assert item.state == "enrolled", item.blocked_reason
            assert len(enrollments) == 1 and verifier.calls == extractor.calls == 1
            enrollment = enrollments[0]
            assert enrollment.source_hypothesis_id == item.result_refs["hypothesis_id"]
            assert enrollment.campaign_version == 1 and enrollment.execution_mode == "automatic"
            assert (await prospecting.get_contact_point(tenant, enrollment.contact_point_id)).verification.value == "verified"
            assert await session.scalar(select(func.count()).select_from(DemandSignalRow).where(DemandSignalRow.tenant_id == tenant)) == 1
            assert await session.scalar(select(func.count()).select_from(NeedHypothesisRow).where(NeedHypothesisRow.tenant_id == tenant)) == 1
        else:
            assert item.state == "blocked"
            assert not enrollments
            assert item.blocked_reason == {"missing_verifier": "verification_unavailable", "sender": "sender_unavailable", "withdrawn": "authorization_changed", "policy": "country_policy_denied", "requirement": "country_policy_requirement_unsupported", "fit": "product_fit_missing", "invalid": "verification_failed", "unknown": "external_reconciliation_required"}[failure]
            if failure not in {"invalid", "unknown"}:
                assert count == 0 and verifier.calls == 0
        assert verifier.calls <= 1 and extractor.calls <= 1


async def test_assignment_checkpoint_rolls_back_account_and_owner(bridge_inventory, monkeypatch):
    import infra.db.customer_preparation_state as state
    from infra.db.tables import OwnershipLockRow, ProspectAccountRow
    consumer, _, _, _, _ = await setup_consumer(bridge_inventory)
    queue, _, factory, tenant, *_ = bridge_inventory
    claim = await queue.claim_preparation(tenant)
    claim = await queue.checkpoint_preparation(tenant, claim, stage="assign")
    original = state.checkpoint
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic checkpoint failure")
    monkeypatch.setattr(state, "checkpoint", fail)
    with pytest.raises(RuntimeError):
        await queue.assign_preparation(tenant, claim)
    async with factory() as session:
        for row_type in (ProspectAccountRow, OwnershipLockRow):
            assert await session.scalar(select(func.count()).select_from(row_type).where(row_type.tenant_id == tenant)) == 0
    monkeypatch.setattr(state, "checkpoint", original)
    current = await queue.assign_preparation(tenant, claim)
    assert current.stage == "evidence" and current.ownership_locked_at is not None
    await consumer.guard(current)


async def test_restart_after_atomic_contact_commit_does_not_duplicate(bridge_inventory):
    import asyncio
    from datetime import timedelta
    consumer, _, extractor, verifier, _ = await setup_consumer(bridge_inventory)
    original = consumer.transactions
    class CrashAfterCommit:
        async def apply(self, claim, action):
            await original.apply(claim, action)
            raise asyncio.CancelledError()
    consumer.transactions = CrashAfterCommit()
    with pytest.raises(asyncio.CancelledError):
        await consumer.scan()
    queue, _, factory, tenant, *_ = bridge_inventory
    queue.now = lambda: NOW + timedelta(minutes=11)
    consumer.transactions = original
    await consumer.scan()
    async with factory() as session:
        item = (await session.scalars(select(CustomerPreparationItemRow).where(CustomerPreparationItemRow.tenant_id == tenant))).one()
        assert item.state == "enrolled", item.blocked_reason
        assert extractor.calls == verifier.calls == 1
        for row_type in (ContactPointRow, DemandSignalRow, NeedHypothesisRow, OutreachEnrollmentRow):
            assert await session.scalar(select(func.count()).select_from(row_type).where(row_type.tenant_id == tenant)) == 1


async def test_evidence_transaction_failure_does_not_leave_partial_profile(bridge_inventory, monkeypatch):
    import infra.db.customer_preparation as transactions
    import infra.db.customer_preparation_state as state
    from infra.db.tables import ProspectAccountRow
    consumer, _, _, _, _ = await setup_consumer(bridge_inventory)
    original = state.checkpoint
    def fail_only_evidence(row, claim, **kwargs):
        if kwargs["stage"] == "verify":
            raise RuntimeError("synthetic commit failure")
        return original(row, claim, **kwargs)
    monkeypatch.setattr(state, "checkpoint", fail_only_evidence)
    assert transactions.PostgresAgentPreparationTransactions
    await consumer.scan()
    _, _, factory, tenant, *_ = bridge_inventory
    async with factory() as session:
        for row_type in (ContactPointRow, DemandSignalRow, NeedHypothesisRow):
            assert await session.scalar(select(func.count()).select_from(row_type).where(row_type.tenant_id == tenant)) == 0
        account = (await session.scalars(select(ProspectAccountRow).where(ProspectAccountRow.tenant_id == tenant))).one()
        assert account.entity_type is None
        item = (await session.scalars(select(CustomerPreparationItemRow).where(CustomerPreparationItemRow.tenant_id == tenant))).one()
        assert item.blocked_reason == "external_reconciliation_required"


async def test_real_enrollment_send_guard_rechecks_current_country_policy(bridge_inventory):
    from infra.db.customer_preparation_send import PreparationSendBindingReader
    from workflows.customer_preparation.send_guard import (
        CurrentCustomerPreparationSendGuard,
    )
    from workflows.customer_preparation.service import PreparationBlocked
    consumer, _, _, _, _ = await setup_consumer(bridge_inventory)
    await consumer.scan()
    _, _, factory, tenant, *_ = bridge_inventory
    async with factory() as session:
        enrollment = (await session.scalars(select(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id == tenant))).one()
    guard = CurrentCustomerPreparationSendGuard(PreparationSendBindingReader(factory, tenant), consumer.authority, consumer.employees,
        consumer.prospecting, consumer.compliance, consumer.outreach)
    await guard.require(tenant, enrollment.enrollment_id)
    class Denied:
        async def get_country_policy_decision(self, *args, **kwargs):
            return CountryPolicyDecision(country_key="us", action=CountryPolicyAction.COLD_B2B_EMAIL, configured=True, allowed=False,
                active_version_id=new_id("cpp"), content_hash="b" * 64, requirements=())
    guard.compliance = Denied()
    with pytest.raises(PreparationBlocked, match="country_policy_denied"):
        await guard.require(tenant, enrollment.enrollment_id)


@pytest.mark.parametrize("required_reader_fails", [False, True])
async def test_preparation_stops_reading_senders_after_first_eligible(
    bridge_inventory, monkeypatch, required_reader_fails,
):
    consumer, _, extractor, verifier, _ = await setup_consumer(
        bridge_inventory, multiple_senders=True,
    )
    _, _, factory, tenant, *_ = bridge_inventory
    first, second = sorted(consumer.senders.snapshots)
    original = consumer.senders.get_sending_identity_eligibility
    reads = []

    async def current_eligibility(requested_tenant, identity):
        reads.append(identity)
        if identity == second or required_reader_fails:
            raise RuntimeError("synthetic unavailable sender fact")
        return await original(requested_tenant, identity)

    monkeypatch.setattr(consumer.senders, "get_sending_identity_eligibility", current_eligibility)
    await consumer.scan()
    before_restart = tuple(reads)
    await consumer.scan()
    assert tuple(reads) == before_restart
    assert reads and set(reads) == {first}
    async with factory() as session:
        item = (await session.scalars(select(CustomerPreparationItemRow).where(
            CustomerPreparationItemRow.tenant_id == tenant,
        ))).one()
        enrollments = (await session.scalars(select(OutreachEnrollmentRow).where(
            OutreachEnrollmentRow.tenant_id == tenant,
        ))).all()
        if required_reader_fails:
            assert item.state == "blocked" and item.blocked_reason == "preparation_failed"
            assert not enrollments and extractor.calls == verifier.calls == 0
            assert len(reads) == 1
            assert await session.scalar(select(func.count()).select_from(ContactPointRow).where(
                ContactPointRow.tenant_id == tenant,
            )) == 0
        else:
            assert item.state == "enrolled", item.blocked_reason
            assert len(enrollments) == 1
            assert enrollments[0].sending_identity_id == first
            assert extractor.calls == verifier.calls == 1
            assert len(reads) >= 2  # 最终仍由原Outreach再次读取并选择身份。
