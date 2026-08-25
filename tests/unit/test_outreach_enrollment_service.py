"""Enrollment/Attempt 服务的资格、幂等、轮询与发送结果行为。"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from domains.outreach.errors import (
    AccountAlreadyEnrolledError,
    CampaignApprovalRequiredError,
    CampaignNotActiveError,
    CampaignQuotaExceededError,
    ContactNotEligibleError,
    IdempotencyConflictError,
    MessageAttemptConflictError,
    ReplyAlreadyReceivedError,
    SendingIdentityUnavailableError,
    SuppressedError,
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
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SuppressionTarget,
)
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    NeedHypothesisId,
    ProspectAccountId,
    SendingIdentityId,
    SuppressionId,
    TenantId,
    new_id,
)
from tests.outreach_fakes import (
    FakeApprovals,
    FakeAudit,
    FakeAuthorizer,
    FakeContacts,
    FakeReplies,
    FakeSenders,
    FakeStore,
    FakeUowFactory,
    Trace,
    denied_authorizer,
)

_models = importlib.import_module("domains.outreach.models")
Campaign = _models.Campaign
CampaignBoundary = _models.CampaignBoundary
CampaignState = _models.CampaignState
CampaignVersion = _models.CampaignVersion
EnrollmentState = _models.EnrollmentState
EnrollmentStopReason = _models.EnrollmentStopReason
MessageAttemptState = _models.MessageAttemptState
SendFailureCategory = _models.SendFailureCategory
SequenceStepSpec = _models.SequenceStepSpec
StepIntent = _models.StepIntent
SuppressionEntry = _models.SuppressionEntry
SuppressionReason = _models.SuppressionReason

NOW = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)


@dataclass
class Harness:
    service: object
    store: FakeStore
    trace: Trace
    audit: FakeAudit
    approvals: FakeApprovals
    contacts: FakeContacts
    senders: FakeSenders
    replies: FakeReplies
    tenant: TenantId
    campaign_id: CampaignId
    sender_a: SendingIdentityId
    sender_b: SendingIdentityId
    account: ProspectAccountId
    contact: ContactPointId
    boss: Actor


def _contact(
    tenant: TenantId,
    contact: ContactPointId,
    account: ProspectAccountId,
    **overrides: object,
) -> ContactEligibilitySnapshot:
    values = {
        "tenant_id": tenant,
        "contact_point_id": contact,
        "account_id": account,
        "verification": ContactVerificationStatus.VERIFIED,
        "verified_at": NOW,
        "legal_basis": ContactLegalBasis.LEGITIMATE_INTEREST,
        "legal_basis_ref": "basis_ref_1",
        "contact_belongs_to_account": True,
        "country": "US",
        "entity_type": "importer",
        "qualified_categories": frozenset({"hardware"}),
        "observed_at": NOW,
    }
    values.update(overrides)
    return ContactEligibilitySnapshot(**values)  # type: ignore[arg-type]


def _sender(
    tenant: TenantId,
    identity_id: SendingIdentityId,
    **overrides: object,
) -> SendingIdentityEligibilitySnapshot:
    values = {
        "tenant_id": tenant,
        "identity_id": identity_id,
        "role": OutreachSenderRole.COLD_OUTREACH,
        "authentication_passed": True,
        "sendable": True,
        "remaining_slots": 5,
        "observed_at": NOW,
    }
    values.update(overrides)
    return SendingIdentityEligibilitySnapshot(**values)  # type: ignore[arg-type]


def _build(
    *,
    contact_snapshot: ContactEligibilitySnapshot | None = None,
    denied: bool = False,
    new_contact_limit: int = 10,
    message_limit: int = 10,
) -> Harness:
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    sender_a = SendingIdentityId(new_id("sid"))
    sender_b = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    trace = Trace()
    store = FakeStore()
    boss = Actor("boss:test", OutreachScope(level=ScopeLevel.TENANT), "boss")
    boundary = CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(sender_b, sender_a),
        steps=(
            SequenceStepSpec(1, StepIntent.DISCOVERY, 0),
            SequenceStepSpec(2, StepIntent.FOLLOW_UP, 2),
        ),
        daily_new_contact_limit=new_contact_limit,
        daily_total_message_limit=message_limit,
        handoff_triggers=(),
    )
    store.campaigns[campaign_id] = Campaign(
        tenant,
        campaign_id,
        CampaignState.ACTIVE,
        1,
        EmployeeId(new_id("emp")),
        NOW,
        approval_id=str(ApprovalId(new_id("apr"))),
        approved_by=EmployeeId(new_id("emp")),
        approved_at=NOW,
    )
    store.versions[(campaign_id, 1)] = CampaignVersion(
        tenant,
        campaign_id,
        1,
        "Hardware discovery",
        boundary,
        EmployeeId(new_id("emp")),
        NOW,
    )
    snapshot = contact_snapshot or _contact(tenant, contact, account)
    contacts = FakeContacts({(contact, account): snapshot}, trace)
    senders = FakeSenders(
        {
            sender_a: _sender(tenant, sender_a),
            sender_b: _sender(tenant, sender_b),
        },
        trace,
    )
    replies = FakeReplies(
        {
            (contact, account): ReplyStatusSnapshot(
                tenant,
                contact,
                account,
                ReplyState.NO_REPLY,
                None,
                NOW,
            )
        },
        trace,
    )
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant,
        campaign_id,
        1,
        ApprovalId(store.campaigns[campaign_id].approval_id),
        CampaignApprovalState.APPROVED,
        store.campaigns[campaign_id].approved_by,
        NOW,
    )
    audit = FakeAudit(trace)
    authorizer = denied_authorizer(trace) if denied else FakeAuthorizer(trace)
    service_type = importlib.import_module(
        "domains.outreach.service_impl"
    ).OutreachServiceImpl
    service = service_type(
        FakeUowFactory(store, trace),
        contacts,
        senders,
        approvals,
        replies,
        authorizer,
        audit,
        now=lambda: NOW,
    )
    return Harness(
        service,
        store,
        trace,
        audit,
        approvals,
        contacts,
        senders,
        replies,
        tenant,
        campaign_id,
        sender_a,
        sender_b,
        account,
        contact,
        boss,
    )


def _request(harness: Harness, *, key: str = "enroll-key-1") -> EnrollmentCreateRequest:
    return EnrollmentCreateRequest(
        harness.account,
        harness.contact,
        IdempotencyKey(key),
    )


def _system(harness: Harness, enrollment_id: EnrollmentId) -> Actor:
    return Actor(
        "system:test",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment_id}),
        ),
        "system",
    )


@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("enroll", "enrollment:create"),
        ("prepare", "enrollment:prepare_send"),
        ("record_sent", "enrollment:record_sent"),
        ("record_failure", "enrollment:record_failure"),
        ("stop", "enrollment:stop"),
        ("get", "enrollment:read"),
        ("list", "enrollment:list"),
    ],
)
async def test_enrollment_methods_preauthorize_before_clock_provider_or_uow(
    method: str, action: str
) -> None:
    harness = _build(denied=True)
    enrollment_id = EnrollmentId(new_id("enr"))
    attempt_id = MessageAttemptId(new_id("mat"))
    calls = {
        "enroll": lambda: harness.service.enroll(
            harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
        ),
        "prepare": lambda: harness.service.prepare_message_attempt(
            harness.tenant, enrollment_id, actor=harness.boss
        ),
        "record_sent": lambda: harness.service.record_sent(
            harness.tenant, attempt_id, "provider_ref_1", actor=harness.boss
        ),
        "record_failure": lambda: harness.service.record_send_failure(
            harness.tenant,
            attempt_id,
            SendFailureCategory.PROVIDER_TRANSIENT,
            actor=harness.boss,
        ),
        "stop": lambda: harness.service.stop_enrollment(
            harness.tenant,
            enrollment_id,
            EnrollmentStopReason.MANUAL,
            actor=harness.boss,
        ),
        "get": lambda: harness.service.get_enrollment(
            harness.tenant, enrollment_id, actor=harness.boss
        ),
        "list": lambda: harness.service.list_enrollments(
            harness.tenant, harness.boss.scope, limit=10, actor=harness.boss
        ),
    }
    with pytest.raises(PermissionDenied):
        await calls[method]()
    assert harness.trace.calls == [
        ("preauthorize", action),
        ("audit", action, "deny:authorization"),
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        {"tenant_id": TenantId(new_id("tn"))},
        {"contact_point_id": ContactPointId(new_id("cp"))},
        {"account_id": ProspectAccountId(new_id("acc"))},
        {
            "verification": ContactVerificationStatus.RISKY,
            "verified_at": None,
        },
        {"legal_basis": None},
        {"contact_belongs_to_account": False},
        {"country": "DE"},
        {"entity_type": "retailer"},
        {"qualified_categories": frozenset({"unapproved"})},
    ],
)
async def test_enroll_rejects_each_untrusted_contact_snapshot_mutation(
    mutation: dict[str, object],
) -> None:
    harness = _build()
    snapshot = _contact(harness.tenant, harness.contact, harness.account)
    for field, value in mutation.items():
        object.__setattr__(snapshot, field, value)
    harness.contacts.snapshots[(harness.contact, harness.account)] = snapshot
    expected = (
        TenantIsolationViolation
        if "tenant_id" in mutation
        else ContactNotEligibleError
    )
    with pytest.raises(expected):
        await harness.service.enroll(
            harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
        )
    assert harness.store.enrollments == {}
    assert harness.store.quotas == {}
    assert all(record["rule"] != "allow:enrollment:create" for record in harness.audit.records)


async def test_enroll_round_robin_is_canonical_persistent_and_idempotent() -> None:
    harness = _build()
    first = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    campaign = harness.store.campaigns[harness.campaign_id]
    quota_after_first = dict(harness.store.quotas)
    cursor_after_first = campaign.round_robin_cursor
    retry = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    assert retry == first
    assert harness.store.quotas == quota_after_first
    assert harness.store.campaigns[harness.campaign_id].round_robin_cursor == cursor_after_first

    winners = [first.sending_identity_id]
    for index in (2, 3):
        account = ProspectAccountId(new_id("acc"))
        contact = ContactPointId(new_id("cp"))
        harness.contacts.snapshots[(contact, account)] = _contact(
            harness.tenant, contact, account
        )
        result = await harness.service.enroll(
            harness.tenant,
            harness.campaign_id,
            EnrollmentCreateRequest(account, contact, IdempotencyKey(f"enroll-key-{index}")),
            actor=harness.boss,
        )
        winners.append(result.sending_identity_id)
    canonical = sorted((harness.sender_a, harness.sender_b))
    assert winners == [canonical[0], canonical[1], canonical[0]]


async def test_enroll_preserves_exact_source_hypothesis_and_binds_it_to_idempotency() -> None:
    harness = _build()
    hypothesis_id = NeedHypothesisId(new_id("hyp"))
    request = EnrollmentCreateRequest(
        harness.account,
        harness.contact,
        IdempotencyKey("enroll-source-hypothesis"),
        source_hypothesis_id=hypothesis_id,
    )

    created = await harness.service.enroll(
        harness.tenant,
        harness.campaign_id,
        request,
        actor=harness.boss,
    )
    retried = await harness.service.enroll(
        harness.tenant,
        harness.campaign_id,
        request,
        actor=harness.boss,
    )

    assert created.source_hypothesis_id == hypothesis_id
    assert retried == created
    assert (
        harness.store.enrollments[created.enrollment_id].source_hypothesis_id
        == hypothesis_id
    )

    with pytest.raises(IdempotencyConflictError):
        await harness.service.enroll(
            harness.tenant,
            harness.campaign_id,
            EnrollmentCreateRequest(
                harness.account,
                harness.contact,
                IdempotencyKey("enroll-source-hypothesis"),
                source_hypothesis_id=NeedHypothesisId(new_id("hyp")),
            ),
            actor=harness.boss,
        )


async def test_enroll_idempotency_and_active_account_conflicts_fail_closed() -> None:
    harness = _build()
    await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    other_account = ProspectAccountId(new_id("acc"))
    other_contact = ContactPointId(new_id("cp"))
    harness.contacts.snapshots[(other_contact, other_account)] = _contact(
        harness.tenant, other_contact, other_account
    )
    with pytest.raises(IdempotencyConflictError):
        await harness.service.enroll(
            harness.tenant,
            harness.campaign_id,
            EnrollmentCreateRequest(other_account, other_contact, IdempotencyKey("enroll-key-1")),
            actor=harness.boss,
        )
    with pytest.raises(AccountAlreadyEnrolledError):
        await harness.service.enroll(
            harness.tenant,
            harness.campaign_id,
            EnrollmentCreateRequest(harness.account, harness.contact, IdempotencyKey("enroll-key-2")),
            actor=harness.boss,
        )


async def test_enroll_same_key_returns_original_after_enrollment_is_terminal() -> None:
    harness = _build()
    original = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    stopped = await harness.service.stop_enrollment(
        harness.tenant,
        original.enrollment_id,
        EnrollmentStopReason.MANUAL,
        actor=harness.boss,
    )
    quota_before = dict(harness.store.quotas)
    retry = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    assert retry == stopped
    assert harness.store.quotas == quota_before


async def test_enroll_idempotent_winner_with_wrong_tenant_fails_closed() -> None:
    harness = _build()
    original = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    harness.store.enrollments[original.enrollment_id].tenant_id = TenantId(
        new_id("tn")
    )
    with pytest.raises(TenantIsolationViolation):
        await harness.service.enroll(
            harness.tenant,
            harness.campaign_id,
            _request(harness),
            actor=harness.boss,
        )


async def test_enroll_manager_must_match_both_campaign_and_account_resources() -> None:
    allowed = _build()
    allowed.service._authorizer = Phase1OutreachAuthorizer(allowed.tenant)
    manager = Actor(
        "manager:test",
        OutreachScope(
            level=ScopeLevel.MANAGER,
            allowed_campaign_ids=frozenset({allowed.campaign_id}),
            allowed_account_ids=frozenset({allowed.account}),
        ),
        "manager",
    )
    result = await allowed.service.enroll(
        allowed.tenant,
        allowed.campaign_id,
        _request(allowed),
        actor=manager,
    )
    assert result.account_id == allowed.account

    for mismatch in ("campaign", "account"):
        denied = _build()
        denied.service._authorizer = Phase1OutreachAuthorizer(denied.tenant)
        scope = OutreachScope(
            level=ScopeLevel.MANAGER,
            allowed_campaign_ids=frozenset(
                {
                    CampaignId(new_id("cmp"))
                    if mismatch == "campaign"
                    else denied.campaign_id
                }
            ),
            allowed_account_ids=frozenset(
                {
                    ProspectAccountId(new_id("acc"))
                    if mismatch == "account"
                    else denied.account
                }
            ),
        )
        with pytest.raises(PermissionDenied):
            await denied.service.enroll(
                denied.tenant,
                denied.campaign_id,
                _request(denied),
                actor=Actor("manager:test", scope, "manager"),
            )
        assert denied.store.enrollments == {}
        assert denied.store.quotas == {}


@pytest.mark.parametrize("role", ["system", "sales"])
async def test_system_and_sales_cannot_create_enrollment(role: str) -> None:
    harness = _build()
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    if role == "system":
        scope = OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({EnrollmentId(new_id("enr"))}),
        )
    else:
        scope = OutreachScope(
            level=ScopeLevel.SELF,
            allowed_campaign_ids=frozenset({harness.campaign_id}),
        )
    with pytest.raises(PermissionDenied):
        await harness.service.enroll(
            harness.tenant,
            harness.campaign_id,
            _request(harness),
            actor=Actor(f"{role}:test", scope, role),
        )
    assert harness.trace.calls == [
        ("audit", "enrollment:create", "deny:authorization")
    ]


async def test_enroll_skips_ineligible_sender_and_rolls_back_when_none_available() -> None:
    harness = _build()
    canonical = sorted((harness.sender_a, harness.sender_b))
    harness.senders.snapshots[canonical[0]] = _sender(
        harness.tenant, canonical[0], remaining_slots=0
    )
    selected = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    assert selected.sending_identity_id == canonical[1]

    blocked = _build()
    for sender_id in (blocked.sender_a, blocked.sender_b):
        blocked.senders.snapshots[sender_id] = _sender(
            blocked.tenant, sender_id, sendable=False
        )
    with pytest.raises(SendingIdentityUnavailableError):
        await blocked.service.enroll(
            blocked.tenant,
            blocked.campaign_id,
            _request(blocked),
            actor=blocked.boss,
        )
    assert blocked.store.enrollments == {}
    assert blocked.store.quotas == {}
    assert blocked.store.campaigns[blocked.campaign_id].round_robin_cursor == -1


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"tenant_id": TenantId(new_id("tn"))}, TenantIsolationViolation),
        ({"identity_id": SendingIdentityId(new_id("sid"))}, SendingIdentityUnavailableError),
        ({"role": OutreachSenderRole.PRIMARY_BUSINESS}, SendingIdentityUnavailableError),
        ({"authentication_passed": False}, SendingIdentityUnavailableError),
        ({"sendable": False}, SendingIdentityUnavailableError),
        ({"remaining_slots": 0}, SendingIdentityUnavailableError),
    ],
)
async def test_enroll_rejects_each_sender_snapshot_mutation_and_rolls_back(
    mutation: dict[str, object], expected: type[Exception]
) -> None:
    harness = _build()
    for identity_id in (harness.sender_a, harness.sender_b):
        snapshot = _sender(harness.tenant, identity_id)
        for field, value in mutation.items():
            object.__setattr__(snapshot, field, value)
        harness.senders.snapshots[identity_id] = snapshot
    with pytest.raises(expected):
        await harness.service.enroll(
            harness.tenant,
            harness.campaign_id,
            _request(harness),
            actor=harness.boss,
        )
    assert harness.store.enrollments == {}
    assert harness.store.quotas == {}
    assert harness.store.campaigns[harness.campaign_id].round_robin_cursor == -1


async def test_prepare_is_not_send_authorization_and_uses_stable_key() -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    attempt = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=_system(harness, enrollment.enrollment_id)
    )
    assert attempt.idempotency_key == IdempotencyKey(
        f"{harness.tenant}:{harness.campaign_id}:{enrollment.enrollment_id}:v1:step1"
    )
    assert attempt.state is MessageAttemptState.RESERVED
    assert not hasattr(attempt, "authorized")
    assert harness.store.quotas[(harness.campaign_id, NOW.date())] == (1, 1)
    retry = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=_system(harness, enrollment.enrollment_id)
    )
    assert retry == attempt
    assert harness.store.quotas[(harness.campaign_id, NOW.date())] == (1, 1)


async def test_prepare_requires_current_campaign_exact_approval_binding() -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    snapshot = harness.approvals.values[(harness.campaign_id, 1)]
    harness.approvals.values[(harness.campaign_id, 1)] = CampaignApprovalSnapshot(
        snapshot.tenant_id,
        snapshot.campaign_id,
        snapshot.version,
        ApprovalId(new_id("apr")),
        snapshot.state,
        snapshot.approved_by,
        snapshot.approved_at,
    )
    with pytest.raises(CampaignApprovalRequiredError):
        await harness.service.prepare_message_attempt(
            harness.tenant,
            enrollment.enrollment_id,
            actor=_system(harness, enrollment.enrollment_id),
        )
    assert harness.store.attempts == {}


async def test_prepare_commits_reply_or_suppression_stop_then_raises_without_allow() -> None:
    for blocked_by in ("reply", "suppression"):
        harness = _build()
        enrollment = await harness.service.enroll(
            harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
        )
        audit_before = len(harness.audit.records)
        if blocked_by == "reply":
            harness.replies.snapshots[(harness.contact, harness.account)] = ReplyStatusSnapshot(
                harness.tenant,
                harness.contact,
                harness.account,
                ReplyState.REPLIED,
                NOW,
                NOW,
            )
            expected = ReplyAlreadyReceivedError
            state = EnrollmentState.REPLIED
        else:
            harness.store.suppressions.append(
                SuppressionEntry(
                    harness.tenant,
                    SuppressionId(new_id("sup")),
                    SuppressionTarget(account_id=harness.account),
                    SuppressionReason.MANUAL_BLOCK,
                    NOW,
                    "manual_ref_1",
                    IdempotencyKey("suppression-key-1"),
                    NOW,
                )
            )
            expected = SuppressedError
            state = EnrollmentState.STOPPED_SUPPRESSED
        with pytest.raises(expected):
            await harness.service.prepare_message_attempt(
                harness.tenant,
                enrollment.enrollment_id,
                actor=_system(harness, enrollment.enrollment_id),
            )
        assert harness.store.enrollments[enrollment.enrollment_id].state is state
        assert harness.store.attempts == {}
        assert len(harness.audit.records) == audit_before


async def test_prepare_quota_cap_rolls_back_attempt_and_preserves_enrollment() -> None:
    harness = _build(new_contact_limit=1, message_limit=1)
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    harness.store.quotas[(harness.campaign_id, NOW.date())] = (1, 1)
    with pytest.raises(CampaignQuotaExceededError):
        await harness.service.prepare_message_attempt(
            harness.tenant,
            enrollment.enrollment_id,
            actor=_system(harness, enrollment.enrollment_id),
        )
    assert harness.store.attempts == {}
    assert harness.store.enrollments[enrollment.enrollment_id].state is EnrollmentState.ENROLLED


@pytest.mark.parametrize("blocker", ["paused", "not_due", "sender_unavailable"])
async def test_prepare_rejects_current_runtime_blockers_without_attempt_or_message_quota(
    blocker: str,
) -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    if blocker == "paused":
        harness.store.campaigns[harness.campaign_id].state = CampaignState.PAUSED
        expected = CampaignNotActiveError
    elif blocker == "not_due":
        harness.store.enrollments[enrollment.enrollment_id].next_send_at = NOW + timedelta(days=1)
        expected = InvalidStateTransition
    else:
        harness.senders.snapshots[enrollment.sending_identity_id] = _sender(
            harness.tenant,
            enrollment.sending_identity_id,
            sendable=False,
        )
        expected = SendingIdentityUnavailableError
    with pytest.raises(expected):
        await harness.service.prepare_message_attempt(
            harness.tenant,
            enrollment.enrollment_id,
            actor=_system(harness, enrollment.enrollment_id),
        )
    assert harness.store.attempts == {}
    assert harness.store.quotas[(harness.campaign_id, NOW.date())] == (1, 0)


async def test_prepare_rejects_corrupted_existing_attempt_without_new_quota() -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    actor = _system(harness, enrollment.enrollment_id)
    attempt = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    harness.store.attempts[attempt.attempt_id].sending_identity_id = SendingIdentityId(
        new_id("sid")
    )
    with pytest.raises(MessageAttemptConflictError):
        await harness.service.prepare_message_attempt(
            harness.tenant, enrollment.enrollment_id, actor=actor
        )
    assert harness.store.quotas[(harness.campaign_id, NOW.date())] == (1, 1)


async def test_prepare_uses_enrollment_version_steps_and_current_version_quota() -> None:
    harness = _build(new_contact_limit=2, message_limit=2)
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    campaign = harness.store.campaigns[harness.campaign_id]
    campaign.current_version = 2
    campaign.approval_id = str(ApprovalId(new_id("apr")))
    current = CampaignVersion(
        harness.tenant,
        harness.campaign_id,
        2,
        "Current one-step boundary",
        CampaignBoundary(
            markets=("US",),
            target_entity_types=("importer",),
            allowed_categories=("hardware",),
            sender_identity_ids=(harness.sender_a, harness.sender_b),
            steps=(SequenceStepSpec(1, StepIntent.DISCOVERY, 0),),
            daily_new_contact_limit=1,
            daily_total_message_limit=2,
            handoff_triggers=(),
        ),
        EmployeeId(new_id("emp")),
        NOW,
    )
    harness.store.versions[(harness.campaign_id, 2)] = current
    actor = _system(harness, enrollment.enrollment_id)
    first = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    await harness.service.claim_message_send(
        harness.tenant, first.attempt_id, actor=actor
    )
    await harness.service.record_sent(
        harness.tenant, first.attempt_id, "provider_ref_1", actor=actor
    )
    assert harness.store.enrollments[enrollment.enrollment_id].state is EnrollmentState.IN_SEQUENCE
    harness.store.enrollments[enrollment.enrollment_id].next_send_at = NOW
    second = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    assert (second.campaign_version, second.step_number) == (1, 2)
    assert harness.store.quotas[(harness.campaign_id, NOW.date())] == (1, 2)


@pytest.mark.parametrize(
    "provider_ref",
    [" bearer_abc", "token_secret", "https://provider.invalid/1", "a@b", "line\nbreak"],
)
async def test_record_sent_rejects_unsafe_provider_ref_without_write(
    provider_ref: str,
) -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    actor = _system(harness, enrollment.enrollment_id)
    attempt = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    actions_before = dict(harness.store.actions)
    with pytest.raises(ValidationError):
        await harness.service.record_sent(
            harness.tenant, attempt.attempt_id, provider_ref, actor=actor
        )
    assert harness.store.attempts[attempt.attempt_id].state is MessageAttemptState.RESERVED
    assert harness.store.actions == actions_before


async def test_record_sent_authoritative_transaction_locks_campaign_then_enrollment_then_attempt() -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    actor = _system(harness, enrollment.enrollment_id)
    attempt = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    await harness.service.claim_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    harness.trace.calls.clear()
    await harness.service.record_sent(
        harness.tenant, attempt.attempt_id, "provider_ref_1", actor=actor
    )
    lock_calls = [call[0] for call in harness.trace.calls if call[0].endswith("lock")]
    assert lock_calls[-3:] == ["campaign_lock", "enrollment_lock", "attempt_lock"]


async def test_record_sent_advances_then_completes_and_emits_once() -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    actor = _system(harness, enrollment.enrollment_id)
    first = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    await harness.service.claim_message_send(
        harness.tenant, first.attempt_id, actor=actor
    )
    sent = await harness.service.record_sent(
        harness.tenant, first.attempt_id, "provider_ref_1", actor=actor
    )
    stored = harness.store.enrollments[enrollment.enrollment_id]
    assert sent.state is MessageAttemptState.SENT
    assert (stored.state, stored.current_step, stored.next_send_at) == (
        EnrollmentState.IN_SEQUENCE,
        1,
        NOW + timedelta(days=2),
    )
    retry = await harness.service.record_sent(
        harness.tenant, first.attempt_id, "provider_ref_1", actor=actor
    )
    assert retry == sent
    assert len(harness.store.events) == 1
    with pytest.raises(MessageAttemptConflictError):
        await harness.service.record_sent(
            harness.tenant, first.attempt_id, "provider_ref_other", actor=actor
        )

    harness.store.enrollments[enrollment.enrollment_id].next_send_at = NOW
    second = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    await harness.service.claim_message_send(
        harness.tenant, second.attempt_id, actor=actor
    )
    await harness.service.record_sent(
        harness.tenant, second.attempt_id, "provider_ref_2", actor=actor
    )
    assert harness.store.enrollments[enrollment.enrollment_id].state is EnrollmentState.COMPLETED
    assert len(harness.store.events) == 2


async def test_record_failure_keeps_transient_retryable_and_stops_unavailable_identity() -> None:
    transient = _build()
    enrollment = await transient.service.enroll(
        transient.tenant,
        transient.campaign_id,
        _request(transient),
        actor=transient.boss,
    )
    actor = _system(transient, enrollment.enrollment_id)
    attempt = await transient.service.prepare_message_attempt(
        transient.tenant, enrollment.enrollment_id, actor=actor
    )
    await transient.service.claim_message_send(
        transient.tenant, attempt.attempt_id, actor=actor
    )
    failed = await transient.service.record_send_failure(
        transient.tenant,
        attempt.attempt_id,
        SendFailureCategory.PROVIDER_TRANSIENT,
        actor=actor,
    )
    assert failed.state is MessageAttemptState.FAILED_TRANSIENT
    assert transient.store.enrollments[enrollment.enrollment_id].state is EnrollmentState.ENROLLED
    assert await transient.service.prepare_message_attempt(
        transient.tenant, enrollment.enrollment_id, actor=actor
    ) == failed

    permanent = _build()
    enrollment = await permanent.service.enroll(
        permanent.tenant,
        permanent.campaign_id,
        _request(permanent),
        actor=permanent.boss,
    )
    actor = _system(permanent, enrollment.enrollment_id)
    attempt = await permanent.service.prepare_message_attempt(
        permanent.tenant, enrollment.enrollment_id, actor=actor
    )
    await permanent.service.claim_message_send(
        permanent.tenant, attempt.attempt_id, actor=actor
    )
    failed = await permanent.service.record_send_failure(
        permanent.tenant,
        attempt.attempt_id,
        SendFailureCategory.IDENTITY_UNAVAILABLE,
        actor=actor,
    )
    assert failed.state is MessageAttemptState.FAILED_PERMANENT
    assert permanent.store.enrollments[enrollment.enrollment_id].state is EnrollmentState.STOPPED_IDENTITY_UNAVAILABLE


async def test_record_failure_and_stop_reject_cross_tenant_authoritative_rows() -> None:
    failure = _build()
    enrollment = await failure.service.enroll(
        failure.tenant, failure.campaign_id, _request(failure), actor=failure.boss
    )
    actor = _system(failure, enrollment.enrollment_id)
    attempt = await failure.service.prepare_message_attempt(
        failure.tenant, enrollment.enrollment_id, actor=actor
    )
    failure.store.attempts[attempt.attempt_id].tenant_id = TenantId(new_id("tn"))
    with pytest.raises(TenantIsolationViolation):
        await failure.service.record_send_failure(
            failure.tenant,
            attempt.attempt_id,
            SendFailureCategory.PROVIDER_TRANSIENT,
            actor=actor,
        )
    assert failure.store.attempts[attempt.attempt_id].state is MessageAttemptState.RESERVED

    stopped = _build()
    enrollment = await stopped.service.enroll(
        stopped.tenant, stopped.campaign_id, _request(stopped), actor=stopped.boss
    )
    stopped.store.enrollments[enrollment.enrollment_id].tenant_id = TenantId(new_id("tn"))
    with pytest.raises(TenantIsolationViolation):
        await stopped.service.stop_enrollment(
            stopped.tenant,
            enrollment.enrollment_id,
            EnrollmentStopReason.MANUAL,
            actor=stopped.boss,
        )
    assert stopped.store.enrollments[enrollment.enrollment_id].state is EnrollmentState.ENROLLED


async def test_stop_get_and_list_enrollments_return_copies_and_commit_then_allow() -> None:
    harness = _build()
    enrollment = await harness.service.enroll(
        harness.tenant, harness.campaign_id, _request(harness), actor=harness.boss
    )
    stopped = await harness.service.stop_enrollment(
        harness.tenant,
        enrollment.enrollment_id,
        EnrollmentStopReason.MANUAL,
        actor=harness.boss,
    )
    fetched = await harness.service.get_enrollment(
        harness.tenant, enrollment.enrollment_id, actor=harness.boss
    )
    listed = await harness.service.list_enrollments(
        harness.tenant, harness.boss.scope, limit=10, actor=harness.boss
    )
    assert stopped.state is EnrollmentState.STOPPED_MANUAL
    assert fetched == stopped
    assert listed == [stopped]
    assert harness.trace.calls[-1] == ("audit", "enrollment:list", "allow:enrollment:list")
