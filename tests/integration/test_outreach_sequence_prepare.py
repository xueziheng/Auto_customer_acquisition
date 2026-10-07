"""``prepare_send`` / ``list_due_sequence_enrollments`` 通过真实 PostgreSQL 验收。

TDD RED：``prepare_send`` 与 due 扫描尚不存在，以下断言全部失败。
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
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
    DraftContent,
    EnrollmentCreateRequest,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendDenialReason,
    SendingIdentityEligibilitySnapshot,
)
from shared.errors import ValidationError
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
EnrollmentStopReason = _models.EnrollmentStopReason
CampaignBoundary = _models.CampaignBoundary
CampaignState = _models.CampaignState
CampaignVersion = _models.CampaignVersion
EnrollmentState = _models.EnrollmentState
EnrollmentStopReason = _models.EnrollmentStopReason
SequenceStepSpec = _models.SequenceStepSpec
StepIntent = _models.StepIntent

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
APPROVER = EmployeeId(new_id("emp"))
DRAFT = DraftContent(subject="demo-subject-marker", body="demo-body-marker")


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture
async def sequence_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_campaign(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    senders: tuple[SendingIdentityId, ...],
    approval_id: ApprovalId,
    *,
    state: CampaignState = CampaignState.ACTIVE,
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
    campaign = Campaign(
        tenant,
        campaign_id,
        state,
        1,
        APPROVER,
        NOW,
        approval_id=str(approval_id),
        approved_by=APPROVER,
        approved_at=NOW,
    )
    version = CampaignVersion(tenant, campaign_id, 1, "Sequence prepare", boundary, APPROVER, NOW)
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)


def _service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender_ids: tuple[SendingIdentityId, ...],
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot],
    clock: MutableClock,
    *,
    sender_sendable: bool = True,
    senders: FakeSenders | None = None,
):
    trace = Trace()
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant, campaign_id, 1, approval_id, CampaignApprovalState.APPROVED, APPROVER, NOW
    )
    sender_provider = senders or FakeSenders(
        {
            sender: SendingIdentityEligibilitySnapshot(
                tenant, sender, OutreachSenderRole.COLD_OUTREACH,
                True, sender_sendable, 100, NOW,
            )
            for sender in sender_ids
        },
        trace,
    )
    reply_provider = FakeReplies(replies, trace)
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    service_type = importlib.import_module("domains.outreach.service_impl").OutreachServiceImpl
    return service_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        FakeContacts(contacts, trace),
        sender_provider,
        approvals,
        reply_provider,
        Phase1OutreachAuthorizer(tenant),
        FakeAudit(trace),
        now=clock.now,
    )


def _verified_contact(
    tenant: TenantId, contact: ContactPointId, account: ProspectAccountId
) -> ContactEligibilitySnapshot:
    return ContactEligibilitySnapshot(
        tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
        ContactLegalBasis.LEGITIMATE_INTEREST, "basis_ref_seq", True,
        "US", "importer", frozenset({"hardware"}), NOW,
    )


def _no_reply(tenant: TenantId, contact: ContactPointId, account: ProspectAccountId) -> ReplyStatusSnapshot:
    return ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)


async def _enroll_and_system(
    service: object,
    tenant: TenantId,
    campaign_id: CampaignId,
    contact: ContactPointId,
    account: ProspectAccountId,
    key: str,
) -> tuple[object, Actor]:
    boss = Actor("boss:sequence", OutreachScope(level=ScopeLevel.TENANT), "boss")
    enrollment = await service.enroll(  # type: ignore[attr-defined]
        tenant, campaign_id,
        EnrollmentCreateRequest(account, contact, IdempotencyKey(key)),
        actor=boss,
    )
    system = Actor(
        "system:sequence",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
        ),
        "system",
    )
    return enrollment, system


async def test_prepare_send_authorizes_with_draft_and_is_idempotent(
    sequence_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(sequence_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {(contact, account): _verified_contact(tenant, contact, account)}
    replies = {(contact, account): _no_reply(tenant, contact, account)}
    await _seed_campaign(factory, tenant, campaign_id, (sender,), approval_id)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, campaign_id, approval_id, (sender,), contacts, replies, clock)
    enrollment, system = await _enroll_and_system(
        service, tenant, campaign_id, contact, account, "seq-enroll-1"
    )

    first = await service.prepare_send(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, DRAFT, actor=system
    )
    assert first.authorized is True
    assert first.denial_reason is None
    assert first.authorization is not None
    assert first.authorization.subject == "demo-subject-marker"
    assert first.authorization.body == "demo-body-marker"
    assert first.authorization.enrollment_id == enrollment.enrollment_id
    assert first.authorization.step_number == 1
    attempt_id = first.authorization.attempt_id
    key = first.authorization.idempotency_key

    second = await service.prepare_send(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, DRAFT, actor=system
    )
    assert second.authorized is True
    assert second.authorization is not None
    assert second.authorization.attempt_id == attempt_id
    assert second.authorization.idempotency_key == key


async def test_prepare_send_denies_reply_and_transitions_enrollment(
    sequence_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(sequence_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {(contact, account): _verified_contact(tenant, contact, account)}
    replied = ReplyStatusSnapshot(tenant, contact, account, ReplyState.REPLIED, NOW, NOW)
    await _seed_campaign(factory, tenant, campaign_id, (sender,), approval_id)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, campaign_id, approval_id, (sender,), contacts, {(contact, account): replied}, clock)
    enrollment, system = await _enroll_and_system(
        service, tenant, campaign_id, contact, account, "seq-enroll-reply"
    )

    decision = await service.prepare_send(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, DRAFT, actor=system
    )
    assert decision.authorized is False
    assert decision.denial_reason is SendDenialReason.REPLY_RECEIVED
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = await session.get(rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id)))
        assert row.state == "replied"


async def test_prepare_send_denies_quota_exhausted(sequence_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(sequence_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account_a = ProspectAccountId(new_id("acc"))
    contact_a = ContactPointId(new_id("cp"))
    account_b = ProspectAccountId(new_id("acc"))
    contact_b = ContactPointId(new_id("cp"))
    contacts = {
        (contact_a, account_a): _verified_contact(tenant, contact_a, account_a),
        (contact_b, account_b): _verified_contact(tenant, contact_b, account_b),
    }
    replies = {
        (contact_a, account_a): _no_reply(tenant, contact_a, account_a),
        (contact_b, account_b): _no_reply(tenant, contact_b, account_b),
    }
    await _seed_campaign(factory, tenant, campaign_id, (sender,), approval_id)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, campaign_id, approval_id, (sender,), contacts, replies, clock)
    first_enrollment, first_system = await _enroll_and_system(
        service, tenant, campaign_id, contact_a, account_a, "seq-enroll-q1"
    )
    first = await service.prepare_send(  # type: ignore[attr-defined]
        tenant, first_enrollment.enrollment_id, DRAFT, actor=first_system
    )
    assert first.authorized is True
    # 直接占满当日消息额度（域内配额机制），再为第二条 Enrollment 准备
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        for _ in range(7):
            quota = await uow.quotas.reserve_message(
                tenant, campaign_id, NOW.date(), 7
            )
            if quota.status.name == "CAP_REACHED":
                break
    assert quota.status.name == "CAP_REACHED"
    second_enrollment, second_system = await _enroll_and_system(
        service, tenant, campaign_id, contact_b, account_b, "seq-enroll-q2"
    )
    second = await service.prepare_send(  # type: ignore[attr-defined]
        tenant, second_enrollment.enrollment_id, DRAFT, actor=second_system
    )
    assert second.authorized is False
    assert second.denial_reason is SendDenialReason.QUOTA_EXHAUSTED


async def test_prepare_send_denies_not_due_and_terminal(sequence_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(sequence_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {(contact, account): _verified_contact(tenant, contact, account)}
    replies = {(contact, account): _no_reply(tenant, contact, account)}
    await _seed_campaign(factory, tenant, campaign_id, (sender,), approval_id)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, campaign_id, approval_id, (sender,), contacts, replies, clock)
    enrollment, system = await _enroll_and_system(
        service, tenant, campaign_id, contact, account, "seq-enroll-due"
    )
    # 未到期：next_send_at 在未来（相对当前时钟）
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = await session.get(rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id)))
        row.next_send_at = NOW + timedelta(hours=1)
        await session.commit()
    not_due = await service.prepare_send(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, DRAFT, actor=system
    )
    assert not_due.authorized is False
    assert not_due.denial_reason is SendDenialReason.NOT_DUE
    # 终态：域内 stop_enrollment 终止
    await service.stop_enrollment(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, EnrollmentStopReason.MANUAL, actor=system
    )
    terminal = await service.prepare_send(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, DRAFT, actor=system
    )
    assert terminal.authorized is False
    assert terminal.denial_reason is SendDenialReason.ENROLLMENT_TERMINAL


async def test_prepare_send_denies_identity_unavailable_and_inactive_campaign(
    sequence_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(sequence_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {(contact, account): _verified_contact(tenant, contact, account)}
    replies = {(contact, account): _no_reply(tenant, contact, account)}
    await _seed_campaign(factory, tenant, campaign_id, (sender,), approval_id)
    clock = MutableClock(NOW)

    trace = Trace()
    senders = FakeSenders(
        {
            sender: SendingIdentityEligibilitySnapshot(
                tenant, sender, OutreachSenderRole.COLD_OUTREACH,
                True, True, 100, NOW,
            )
        },
        trace,
    )
    service_blocked = _service(
        factory, tenant, campaign_id, approval_id, (sender,), contacts, replies, clock,
        senders=senders,
    )
    enrollment, system = await _enroll_and_system(
        service_blocked, tenant, campaign_id, contact, account, "seq-enroll-ident"
    )
    senders.snapshots[sender] = SendingIdentityEligibilitySnapshot(
        tenant, sender, OutreachSenderRole.COLD_OUTREACH,
        True, False, 100, NOW,
    )
    denied = await service_blocked.prepare_send(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, DRAFT, actor=system
    )
    assert denied.authorized is False
    assert denied.denial_reason is SendDenialReason.IDENTITY_UNAVAILABLE

    paused_campaign = CampaignId(new_id("cmp"))
    paused_approval = ApprovalId(new_id("apr"))
    await _seed_campaign(
        factory, tenant, paused_campaign, (sender,), paused_approval,
        state=CampaignState.ACTIVE,
    )
    service_paused = _service(
        factory, tenant, paused_campaign, paused_approval, (sender,), contacts, replies, clock
    )
    paused_account = ProspectAccountId(new_id("acc"))
    paused_contact = ContactPointId(new_id("cp"))
    contacts[(paused_contact, paused_account)] = _verified_contact(
        tenant, paused_contact, paused_account
    )
    replies[(paused_contact, paused_account)] = _no_reply(
        tenant, paused_contact, paused_account
    )
    paused_enrollment, paused_system = await _enroll_and_system(
        service_paused, tenant, paused_campaign, paused_contact, paused_account, "seq-enroll-paused"
    )
    boss_paused = Actor("boss:sequence", OutreachScope(level=ScopeLevel.TENANT), "boss")
    await service_paused.pause_campaign(  # type: ignore[attr-defined]
        tenant, paused_campaign, "paused-for-test", actor=boss_paused
    )
    inactive = await service_paused.prepare_send(  # type: ignore[attr-defined]
        tenant, paused_enrollment.enrollment_id, DRAFT, actor=paused_system
    )
    assert inactive.authorized is False
    assert inactive.denial_reason is SendDenialReason.CAMPAIGN_NOT_ACTIVE


async def test_list_due_sequence_enrollments_filters_by_state_due_and_campaign(
    sequence_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(sequence_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    active_campaign = CampaignId(new_id("cmp"))
    paused_campaign = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    paused_approval = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    await _seed_campaign(factory, tenant, active_campaign, (sender,), approval_id)
    await _seed_campaign(
        factory, tenant, paused_campaign, (sender,), paused_approval, state=CampaignState.PAUSED
    )
    clock = MutableClock(NOW)
    contacts = {}
    replies = {}
    for index in range(3):
        account = ProspectAccountId(new_id("acc"))
        contact = ContactPointId(new_id("cp"))
        contacts[(contact, account)] = _verified_contact(tenant, contact, account)
        replies[(contact, account)] = _no_reply(tenant, contact, account)
    service = _service(
        factory, tenant, active_campaign, approval_id, (sender,), contacts, replies, clock
    )
    boss = Actor("boss:sequence", OutreachScope(level=ScopeLevel.TENANT), "boss")
    due_rows: list[tuple[object, Actor]] = []
    for index in range(3):
        contact, account = list(contacts)[index]
        enrollment, system = await _enroll_and_system(
            service, tenant, active_campaign, contact, account, f"seq-enroll-due-{index}"
        )
        due_rows.append((enrollment, system))
    # 把第二条 enrollment 置为已回复（终态）→ 不应出现在 due 列表
    second_id = str(due_rows[1][0].enrollment_id)
    await service.stop_enrollment(  # type: ignore[attr-defined]
        tenant, due_rows[1][0].enrollment_id, EnrollmentStopReason.MANUAL, actor=due_rows[1][1]
    )
    # 第三条：下次发送时间在未来 → 不应出现在 due 列表
    future_at = NOW + timedelta(days=1)
    third_id = str(due_rows[2][0].enrollment_id)
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = await session.get(rows.OutreachEnrollmentRow, (str(tenant), third_id))
        row.next_send_at = future_at
        await session.commit()

    due = await service.list_due_sequence_enrollments(  # type: ignore[attr-defined]
        tenant, limit=10, actor=boss
    )
    due_ids = {str(view.enrollment_id) for view in due}
    assert str(due_rows[0][0].enrollment_id) in due_ids
    assert second_id not in due_ids
    assert third_id not in due_ids
    assert len(due) == 1


async def test_automatic_stop_requires_narrow_system_scope_and_is_idempotent(
    sequence_engine: AsyncEngine,
) -> None:
    """``stop_enrollment(REPLY)`` 事件订阅契约：非 SYSTEM 拒绝、SYSTEM 精确单
    enrollment 可停、重复投递幂等。"""
    factory = async_sessionmaker(sequence_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    await _seed_campaign(factory, tenant, campaign_id, (sender,), approval_id)
    clock = MutableClock(NOW)
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {(contact, account): _verified_contact(tenant, contact, account)}
    replies = {(contact, account): _no_reply(tenant, contact, account)}
    service = _service(
        factory, tenant, campaign_id, approval_id, (sender,), contacts, replies, clock
    )
    enrollment, system = await _enroll_and_system(
        service, tenant, campaign_id, contact, account, "seq-enroll-auto-stop"
    )
    # 非 SYSTEM（boss TENANT）不能自动停止
    boss = Actor("boss:sequence", OutreachScope(level=ScopeLevel.TENANT), "boss")
    with pytest.raises(ValidationError):
        await service.stop_enrollment(  # type: ignore[attr-defined]
            tenant, enrollment.enrollment_id, EnrollmentStopReason.REPLY, actor=boss
        )
    # SYSTEM 精确单 enrollment → replied
    view = await service.stop_enrollment(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, EnrollmentStopReason.REPLY, actor=system
    )
    assert view.state.value == "replied"
    assert view.next_send_at is None
    # 重复投递幂等：再次 stop 仍是 replied，不抛错
    again = await service.stop_enrollment(  # type: ignore[attr-defined]
        tenant, enrollment.enrollment_id, EnrollmentStopReason.REPLY, actor=system
    )
    assert again.state.value == "replied"
