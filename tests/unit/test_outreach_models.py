"""触达域纯模型与状态机的行为契约。"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta

import pytest

from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    MessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)


def _models() -> object:
    return importlib.import_module("domains.outreach.models")


def _schemas() -> object:
    return importlib.import_module("domains.outreach.schemas")


NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
CAMPAIGN = CampaignId(new_id("cmp"))
ENROLLMENT = EnrollmentId(new_id("enr"))
ACCOUNT = ProspectAccountId(new_id("acc"))
CONTACT = ContactPointId(new_id("cp"))
SENDER = SendingIdentityId(new_id("sid"))


_CAMPAIGN_TRANSITIONS = {
    "draft": {"pending_approval", "cancelled"},
    "pending_approval": {"active", "cancelled"},
    "active": {"paused", "completed", "cancelled", "pending_approval"},
    "paused": {"active", "completed", "cancelled", "pending_approval"},
    "completed": set(),
    "cancelled": set(),
}


def test_campaign_transition_table_is_closed() -> None:
    """增删任一未批准状态边都会被独立表捕获。"""
    models = _models()
    for source in models.CampaignState:
        for target in models.CampaignState:
            if target.value in _CAMPAIGN_TRANSITIONS[source.value]:
                models.validate_campaign_transition(source, target)
            else:
                with pytest.raises(InvalidStateTransition):
                    models.validate_campaign_transition(source, target)


_ENROLLMENT_TRANSITIONS = {
    "enrolled": {
        "in_sequence",
        "replied",
        "completed",
        "stopped_suppressed",
        "stopped_bounced",
        "stopped_manual",
        "stopped_identity_unavailable",
    },
    "in_sequence": {
        "in_sequence",
        "replied",
        "completed",
        "stopped_suppressed",
        "stopped_bounced",
        "stopped_manual",
        "stopped_identity_unavailable",
    },
    "replied": set(),
    "completed": set(),
    "stopped_suppressed": set(),
    "stopped_bounced": set(),
    "stopped_manual": set(),
    "stopped_identity_unavailable": set(),
}


def test_enrollment_transition_table_is_closed() -> None:
    """任何终态复活或跨过 in-sequence 都会被拒绝。"""
    models = _models()
    for source in models.EnrollmentState:
        for target in models.EnrollmentState:
            if target.value in _ENROLLMENT_TRANSITIONS[source.value]:
                models.validate_enrollment_transition(source, target)
            else:
                with pytest.raises(InvalidStateTransition):
                    models.validate_enrollment_transition(source, target)


_ATTEMPT_TRANSITIONS = {
    "reserved": {"sent", "failed_transient", "failed_permanent"},
    "failed_transient": {"sent", "failed_transient", "failed_permanent"},
    "sent": set(),
    "failed_permanent": set(),
}


def test_message_attempt_transition_table_is_closed() -> None:
    """已发送或永久失败的 attempt 不能再次进入发送路径。"""
    models = _models()
    for source in models.MessageAttemptState:
        for target in models.MessageAttemptState:
            if target.value in _ATTEMPT_TRANSITIONS[source.value]:
                models.validate_message_attempt_transition(source, target)
            else:
                with pytest.raises(InvalidStateTransition):
                    models.validate_message_attempt_transition(source, target)


def _valid_boundary(**changes: object) -> object:
    models = _models()
    values: dict[str, object] = {
        "markets": ["US", "DE"],
        "target_entity_types": ["importer"],
        "allowed_categories": ["hardware"],
        "sender_identity_ids": [SENDER],
        "steps": [
            models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0),
            models.SequenceStepSpec(2, models.StepIntent.FOLLOW_UP, 2),
        ],
        "daily_new_contact_limit": 5,
        "daily_total_message_limit": 10,
        "handoff_triggers": ["quote_requested"],
        "stop_on_reply": True,
    }
    values.update(changes)
    return models.CampaignBoundary(**values)


def test_campaign_boundary_defensively_copies_and_canonicalizes_collections() -> None:
    """调用方后改 list 不能偷偷扩大老板已批准边界。"""
    markets = ["US", "DE"]
    senders = [SENDER]
    boundary = _valid_boundary(markets=markets, sender_identity_ids=senders)
    markets.append("CN")
    senders.append(SendingIdentityId(new_id("sid")))
    assert boundary.markets == ("DE", "US")
    assert boundary.sender_identity_ids == (SENDER,)
    with pytest.raises(AttributeError):
        boundary.markets.append("CN")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"markets": []}, "markets_required"),
        ({"target_entity_types": []}, "target_entity_types_required"),
        ({"allowed_categories": []}, "allowed_categories_required"),
        ({"sender_identity_ids": []}, "sender_identity_ids_required"),
        ({"steps": []}, "steps_required"),
        ({"daily_new_contact_limit": 0}, "daily_new_contact_limit_positive"),
        ({"daily_total_message_limit": 0}, "daily_total_message_limit_positive"),
        (
            {"daily_new_contact_limit": 11, "daily_total_message_limit": 10},
            "new_contact_limit_exceeds_total",
        ),
        ({"stop_on_reply": False}, "stop_on_reply_required"),
    ],
)
def test_campaign_boundary_rejects_each_independent_violation(
    changes: dict[str, object], code: str
) -> None:
    """任一空边界、非正额度或 stop-on-reply 旁路都不能构造。"""
    with pytest.raises(ValidationError, match=code):
        _valid_boundary(**changes)


def test_campaign_boundary_rejects_bad_step_shape_and_unknown_trigger() -> None:
    """步骤编号、首步意图、等待天数与转人工词表必须封闭。"""
    models = _models()
    invalid_steps = [
        [models.SequenceStepSpec(1, models.StepIntent.FOLLOW_UP, 0)],
        [models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 1)],
        [
            models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0),
            models.SequenceStepSpec(3, models.StepIntent.FOLLOW_UP, 1),
        ],
        [
            models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0),
            models.SequenceStepSpec(2, models.StepIntent.FOLLOW_UP, 0),
        ],
        [
            models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0),
            *[
                models.SequenceStepSpec(number, models.StepIntent.FOLLOW_UP, 1)
                for number in range(2, 7)
            ],
        ],
    ]
    for steps in invalid_steps:
        with pytest.raises(ValidationError):
            _valid_boundary(steps=steps)
    with pytest.raises(ValidationError, match="handoff_trigger_invalid"):
        _valid_boundary(handoff_triggers=["free_text_trigger"])


def _enrollment(state: object | None = None) -> object:
    models = _models()
    return models.Enrollment(
        tenant_id=TENANT,
        enrollment_id=ENROLLMENT,
        campaign_id=CAMPAIGN,
        campaign_version=1,
        account_id=ACCOUNT,
        contact_point_id=CONTACT,
        sending_identity_id=SENDER,
        state=state or models.EnrollmentState.ENROLLED,
        current_step=0,
        next_send_at=NOW,
        enrolled_at=NOW,
        stopped_at=None,
        stop_reason=None,
        idempotency_key=IdempotencyKey("enroll-key-1"),
    )


def test_enrollment_transition_requires_typed_stop_reason_and_time() -> None:
    """终止原因不能与状态矛盾，也不能留下 naive 时间。"""
    models = _models()
    enrollment = _enrollment()
    enrollment.transition_to(
        models.EnrollmentState.STOPPED_SUPPRESSED,
        at=NOW,
        reason=models.EnrollmentStopReason.SUPPRESSION,
    )
    assert enrollment.state is models.EnrollmentState.STOPPED_SUPPRESSED
    assert enrollment.stop_reason is models.EnrollmentStopReason.SUPPRESSION
    with pytest.raises(InvalidStateTransition):
        enrollment.transition_to(models.EnrollmentState.IN_SEQUENCE, at=NOW)
    with pytest.raises(ValidationError):
        _enrollment(models.EnrollmentState.STOPPED_MANUAL)


def _attempt(**changes: object) -> object:
    models = _models()
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "attempt_id": MessageAttemptId(new_id("mat")),
        "message_id": MessageId(new_id("msg")),
        "campaign_id": CAMPAIGN,
        "enrollment_id": ENROLLMENT,
        "campaign_version": 1,
        "step_number": 1,
        "sending_identity_id": SENDER,
        "idempotency_key": IdempotencyKey("attempt-key-1"),
        "state": models.MessageAttemptState.RESERVED,
        "provider_ref": None,
        "failure_category": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return models.MessageAttempt(**values)


def test_attempt_state_requires_matching_provider_and_failure_fields() -> None:
    """provider ref 与失败类别不能出现在错误的 attempt 状态。"""
    models = _models()
    _attempt(
        state=models.MessageAttemptState.SENT,
        provider_ref="provider_ref_1",
    )
    _attempt(
        state=models.MessageAttemptState.FAILED_TRANSIENT,
        failure_category=models.SendFailureCategory.PROVIDER_TRANSIENT,
    )
    with pytest.raises(ValidationError):
        _attempt(state=models.MessageAttemptState.SENT, provider_ref=None)
    with pytest.raises(ValidationError):
        _attempt(
            state=models.MessageAttemptState.RESERVED,
            failure_category=models.SendFailureCategory.PROVIDER_TRANSIENT,
        )


def test_daily_quota_usage_rejects_bool_negative_and_naive_date_shapes() -> None:
    """计数器初始形态不能接受 bool/负数或字符串日期。"""
    models = _models()
    valid = models.DailyQuotaUsage(
        tenant_id=TENANT,
        campaign_id=CAMPAIGN,
        on_day=date(2026, 8, 11),
        new_contacts_reserved=5,
        messages_reserved=3,
    )
    assert valid.new_contacts_reserved == 5
    for value in (-1, True):
        with pytest.raises(ValidationError):
            models.DailyQuotaUsage(
                tenant_id=TENANT,
                campaign_id=CAMPAIGN,
                on_day=date(2026, 8, 11),
                new_contacts_reserved=value,
                messages_reserved=0,
            )


def test_campaign_version_is_frozen_and_revision_does_not_mutate_v1() -> None:
    """创建 v2 不能改变已批准 v1 的名称或边界。"""
    models = _models()
    v1 = models.CampaignVersion(
        tenant_id=TENANT,
        campaign_id=CAMPAIGN,
        version=1,
        name="Cold hardware discovery",
        boundary=_valid_boundary(),
        created_by=EmployeeId(new_id("emp")),
        created_at=NOW,
    )
    v2 = v1.revise(
        name="Cold packaging discovery",
        boundary=_valid_boundary(allowed_categories=["packaging"]),
        created_at=NOW + timedelta(minutes=1),
    )
    assert v1.version == 1
    assert v1.name == "Cold hardware discovery"
    assert v1.boundary.allowed_categories == ("hardware",)
    assert v2.version == 2
    assert v2.boundary.allowed_categories == ("packaging",)
    with pytest.raises(AttributeError):
        v1.name = "mutated"


def test_suppression_entry_keeps_occurrence_and_creation_times_distinct() -> None:
    """外部事实时间不能覆盖服务端落库时间。"""
    models = _models()
    schemas = _schemas()
    identifiers = importlib.import_module("shared.schemas.identifiers")
    target = schemas.SuppressionTarget(contact_point_id=CONTACT)
    entry = models.SuppressionEntry(
        tenant_id=TENANT,
        suppression_id=identifiers.SuppressionId(new_id("sup")),
        target=target,
        reason=models.SuppressionReason.COMPLAINT,
        occurred_at=NOW - timedelta(minutes=3),
        source_ref="msg_event_1",
        idempotency_key=IdempotencyKey("suppression-key-1"),
        created_at=NOW,
    )
    assert entry.occurred_at < entry.created_at
