"""Enrollment 与 Message Attempt 通过真实 PostgreSQL UoW 持久化。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.outreach.permissions import (
    Actor,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.outreach_fakes import (
    FakeApprovals,
    FakeAudit,
    FakeContacts,
    FakeReplies,
    FakeSenders,
    Trace,
)

_models = importlib.import_module("domains.outreach.models")
Campaign = _models.Campaign
CampaignBoundary = _models.CampaignBoundary
CampaignState = _models.CampaignState
CampaignVersion = _models.CampaignVersion
EnrollmentState = _models.EnrollmentState
MessageAttemptState = _models.MessageAttemptState
SequenceStepSpec = _models.SequenceStepSpec
StepIntent = _models.StepIntent

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
APPROVER = EmployeeId(new_id("emp"))


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture
async def enrollment_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_active_campaign(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    senders: tuple[SendingIdentityId, ...],
    approval_id: ApprovalId,
    *,
    new_contact_limit: int = 5,
    message_limit: int = 7,
) -> None:
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    boundary = CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=senders,
        steps=(
            SequenceStepSpec(1, StepIntent.DISCOVERY, 0),
            SequenceStepSpec(2, StepIntent.FOLLOW_UP, 2),
        ),
        daily_new_contact_limit=new_contact_limit,
        daily_total_message_limit=message_limit,
        handoff_triggers=(),
    )
    creator = APPROVER
    campaign = Campaign(
        tenant,
        campaign_id,
        CampaignState.ACTIVE,
        1,
        creator,
        NOW,
        approval_id=str(approval_id),
        approved_by=creator,
        approved_at=NOW,
    )
    version = CampaignVersion(
        tenant,
        campaign_id,
        1,
        "Integration discovery",
        boundary,
        creator,
        NOW,
    )
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)


def _service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender_ids: tuple[SendingIdentityId, ...],
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    clock: MutableClock,
    audit: FakeAudit | None = None,
):
    trace = Trace()
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant,
        campaign_id,
        1,
        approval_id,
        CampaignApprovalState.APPROVED,
        APPROVER,
        NOW,
    )
    sender_provider = FakeSenders(
        {
            sender: SendingIdentityEligibilitySnapshot(
                tenant,
                sender,
                OutreachSenderRole.COLD_OUTREACH,
                True,
                True,
                100,
                NOW,
            )
            for sender in sender_ids
        },
        trace,
    )
    replies = FakeReplies(
        {
            key: ReplyStatusSnapshot(
                tenant,
                key[0],
                key[1],
                ReplyState.NO_REPLY,
                None,
                NOW,
            )
            for key in contacts
        },
        trace,
    )
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    service_type = importlib.import_module("domains.outreach.service_impl").OutreachServiceImpl
    return service_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        FakeContacts(contacts, trace),
        sender_provider,
        approvals,
        replies,
        Phase1OutreachAuthorizer(tenant),
        audit or FakeAudit(trace),
        now=clock.now,
    )


async def test_enrollment_attempt_and_two_step_completion_are_durable(
    enrollment_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(enrollment_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    senders = tuple(sorted((SendingIdentityId(new_id("sid")), SendingIdentityId(new_id("sid")))))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant,
            contact,
            account,
            ContactVerificationStatus.VERIFIED,
            NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST,
            "basis_ref_1",
            True,
            "US",
            "importer",
            frozenset({"hardware"}),
            NOW,
        )
    }
    await _seed_active_campaign(factory, tenant, campaign_id, senders, approval_id)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, campaign_id, approval_id, senders, contacts, clock)
    boss = Actor("boss:integration", OutreachScope(level=ScopeLevel.TENANT), "boss")
    enrollment = await service.enroll(
        tenant,
        campaign_id,
        EnrollmentCreateRequest(account, contact, IdempotencyKey("integration-enroll-1")),
        actor=boss,
    )
    system = Actor(
        "system:integration",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
        ),
        "system",
    )
    first = await service.prepare_message_attempt(tenant, enrollment.enrollment_id, actor=system)
    await service.claim_message_send(tenant, first.attempt_id, actor=system)
    await service.record_sent(tenant, first.attempt_id, "provider_ref_1", actor=system)

    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = await session.get(rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id)))
        assert row is not None
        assert (row.state, row.current_step, row.next_send_at) == (
            "in_sequence",
            1,
            NOW + timedelta(days=2),
        )
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachMessageAttemptRow)
            .where(rows.OutreachMessageAttemptRow.tenant_id == tenant)
        ) == 1
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(
                rows.OutboxEventRow.tenant_id == tenant,
                rows.OutboxEventRow.event_type == "MessageSent",
            )
        ) == 1

    clock.value = NOW + timedelta(days=2)
    second = await service.prepare_message_attempt(tenant, enrollment.enrollment_id, actor=system)
    assert second.step_number == 2
    await service.claim_message_send(tenant, second.attempt_id, actor=system)
    await service.record_sent(tenant, second.attempt_id, "provider_ref_2", actor=system)
    retry = await service.enroll(
        tenant,
        campaign_id,
        EnrollmentCreateRequest(
            account, contact, IdempotencyKey("integration-enroll-1")
        ),
        actor=boss,
    )
    assert retry.enrollment_id == enrollment.enrollment_id
    assert retry.state is EnrollmentState.COMPLETED
    async with factory() as session:
        row = await session.get(rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id)))
        assert row is not None
        assert (row.state, row.current_step, row.next_send_at) == (
            EnrollmentState.COMPLETED.value,
            2,
            None,
        )
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(
                rows.OutboxEventRow.tenant_id == tenant,
                rows.OutboxEventRow.event_type == "MessageSent",
            )
        ) == 2
        attempts = (
            await session.execute(
                select(rows.OutreachMessageAttemptRow)
                .where(rows.OutreachMessageAttemptRow.tenant_id == tenant)
                .order_by(rows.OutreachMessageAttemptRow.step_number)
            )
        ).scalars().all()
        assert [attempt.state for attempt in attempts] == [
            MessageAttemptState.SENT.value,
            MessageAttemptState.SENT.value,
        ]


async def test_enrollment_commit_failure_rolls_back_quota_cursor_action_and_allow_audit(
    enrollment_engine: AsyncEngine,
) -> None:
    class CommitFailure(RuntimeError):
        pass

    class CommitFailingSession(AsyncSession):
        async def commit(self) -> None:
            raise CommitFailure()

    normal_factory = async_sessionmaker(enrollment_engine, expire_on_commit=False)
    failing_factory = async_sessionmaker(
        enrollment_engine,
        expire_on_commit=False,
        class_=CommitFailingSession,
    )
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {(contact, account): ContactEligibilitySnapshot(
        tenant,
        contact,
        account,
        ContactVerificationStatus.VERIFIED,
        NOW,
        ContactLegalBasis.LEGITIMATE_INTEREST,
        "basis_ref_1",
        True,
        "US",
        "importer",
        frozenset({"hardware"}),
        NOW,
    )}
    await _seed_active_campaign(
        normal_factory, tenant, campaign_id, (sender,), approval_id
    )
    trace = Trace()
    audit = FakeAudit(trace)
    service = _service(
        failing_factory,
        tenant,
        campaign_id,
        approval_id,
        (sender,),
        contacts,
        MutableClock(NOW),
        audit,
    )
    boss = Actor("boss:integration", OutreachScope(level=ScopeLevel.TENANT), "boss")
    with pytest.raises(CommitFailure):
        await service.enroll(
            tenant,
            campaign_id,
            EnrollmentCreateRequest(account, contact, IdempotencyKey("commit-failure")),
            actor=boss,
        )
    rows = importlib.import_module("infra.db.tables")
    async with normal_factory() as session:
        campaign = await session.get(
            rows.OutreachCampaignRow, (str(tenant), str(campaign_id))
        )
        assert campaign is not None and campaign.round_robin_cursor == -1
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachEnrollmentRow)
            .where(rows.OutreachEnrollmentRow.tenant_id == tenant)
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachDailyQuotaRow)
            .where(rows.OutreachDailyQuotaRow.tenant_id == tenant)
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachActionRow)
            .where(rows.OutreachActionRow.tenant_id == tenant)
        ) == 0
    assert [record for record in audit.records if record["rule"].startswith("allow:")] == []
