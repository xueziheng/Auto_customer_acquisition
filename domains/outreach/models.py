"""触达域实体与显式状态机。

**内部实现，其他域不得导入。** Campaign 边界、版本、Enrollment、
Message Attempt 与抑制事实都只保存安全 typed metadata，不保存地址或正文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime
from enum import Enum
from typing import TYPE_CHECKING

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
    SuppressionId,
    TenantId,
)

if TYPE_CHECKING:
    from domains.outreach.schemas import DeliveryCorrelationBinding, SuppressionTarget


_ULID_ID = re.compile(r"[a-z]+_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_DELIVERY_ROUTE = re.compile(r"[a-z0-9-]{1,32}")
_DELIVERY_DIGEST = re.compile(r"[0-9a-f]{64}")
_DELIVERY_MESSAGE_ID = re.compile(
    r"<([a-z0-9-]{1,32})\.([0-9a-f]{64})@messages\.tradeos\.invalid>"
)
_DELIVERY_IDEMPOTENCY_HEADER = re.compile(
    r"([a-z0-9-]{1,32})\.([0-9a-f]{64})"
)
_CREDENTIAL_MARKERS = ("bearer", "token", "secret", "password")
_HANDOFF_TRIGGERS = frozenset(
    {
        "quantity_provided",
        "target_price_provided",
        "sample_requested",
        "quote_requested",
        "specification_file_received",
        "meeting_requested",
        "custom_product",
        "certification_question",
        "payment_or_contract_terms",
        "complaint",
        "exclusive_distribution",
        "large_account",
    }
)


def _require_utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValidationError(f"{field} 必须是 UTC aware datetime")
    return value


def _require_id(value: object, prefix: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or _ULID_ID.fullmatch(value) is None
        or not value.startswith(f"{prefix}_")
    ):
        raise ValidationError(f"{field} 无效")
    return value


def _validate_delivery_route(value: object) -> str:
    if (
        not isinstance(value, str)
        or _DELIVERY_ROUTE.fullmatch(value) is None
        or any(marker in value for marker in _CREDENTIAL_MARKERS)
    ):
        raise ValidationError("delivery route 无效")
    return value


def _split_deterministic_message_id(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValidationError("deterministic_message_id 无效")
    match = _DELIVERY_MESSAGE_ID.fullmatch(value)
    if match is None:
        raise ValidationError("deterministic_message_id 无效")
    route_id, digest = match.groups()
    _validate_delivery_route(route_id)
    assert _DELIVERY_DIGEST.fullmatch(digest) is not None
    return route_id, digest


def _split_idempotency_header(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValidationError("idempotency_header 无效")
    match = _DELIVERY_IDEMPOTENCY_HEADER.fullmatch(value)
    if match is None:
        raise ValidationError("idempotency_header 无效")
    route_id, digest = match.groups()
    _validate_delivery_route(route_id)
    assert _DELIVERY_DIGEST.fullmatch(digest) is not None
    return route_id, digest


def _freeze_texts(
    value: object,
    *,
    field: str,
    required: bool,
    lower: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValidationError(f"{field} 必须为集合")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise ValidationError(f"{field} 包含无效值")
        canonical = item.lower() if lower else item
        if any(ord(character) < 32 or ord(character) == 127 for character in canonical):
            raise ValidationError(f"{field} 包含无效值")
        cleaned.append(canonical)
    result = tuple(sorted(set(cleaned)))
    if required and not result:
        raise ValidationError(f"{field}_required")
    return result


class CampaignState(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StepIntent(str, Enum):
    DISCOVERY = "discovery"
    PRESENTATION = "presentation"
    FOLLOW_UP = "follow_up"


class EnrollmentState(str, Enum):
    ENROLLED = "enrolled"
    IN_SEQUENCE = "in_sequence"
    REPLIED = "replied"
    COMPLETED = "completed"
    STOPPED_SUPPRESSED = "stopped_suppressed"
    STOPPED_BOUNCED = "stopped_bounced"
    STOPPED_MANUAL = "stopped_manual"
    STOPPED_IDENTITY_UNAVAILABLE = "stopped_identity_unavailable"


class EnrollmentStopReason(str, Enum):
    REPLY = "reply"
    SUPPRESSION = "suppression"
    HARD_BOUNCE = "hard_bounce"
    MANUAL = "manual"
    IDENTITY_UNAVAILABLE = "identity_unavailable"


class SuppressionScope(str, Enum):
    CONTACT = "contact"
    ACCOUNT = "account"


class SuppressionReason(str, Enum):
    UNSUBSCRIBE = "unsubscribe"
    COMPLAINT = "complaint"
    HARD_BOUNCE = "hard_bounce"
    MANUAL_BLOCK = "manual_block"
    COMPETITOR = "competitor"
    EXISTING_CUSTOMER_CONFLICT = "existing_customer_conflict"


class MessageAttemptState(str, Enum):
    RESERVED = "reserved"
    SENDING = "sending"
    SENT = "sent"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"


class SendFailureCategory(str, Enum):
    RATE_LIMITED = "rate_limited"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_AUTH_REQUIRED = "provider_auth_required"
    PROVIDER_PERMANENT = "provider_permanent"
    IDENTITY_UNAVAILABLE = "identity_unavailable"


class DeliveryFeedbackKind(str, Enum):
    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"


_CAMPAIGN_TRANSITIONS: dict[CampaignState, frozenset[CampaignState]] = {
    CampaignState.DRAFT: frozenset(
        {CampaignState.PENDING_APPROVAL, CampaignState.CANCELLED}
    ),
    CampaignState.PENDING_APPROVAL: frozenset(
        {CampaignState.ACTIVE, CampaignState.CANCELLED}
    ),
    CampaignState.ACTIVE: frozenset(
        {
            CampaignState.PAUSED,
            CampaignState.COMPLETED,
            CampaignState.CANCELLED,
            CampaignState.PENDING_APPROVAL,
        }
    ),
    CampaignState.PAUSED: frozenset(
        {
            CampaignState.ACTIVE,
            CampaignState.COMPLETED,
            CampaignState.CANCELLED,
            CampaignState.PENDING_APPROVAL,
        }
    ),
    CampaignState.COMPLETED: frozenset(),
    CampaignState.CANCELLED: frozenset(),
}


_ENROLLMENT_TERMINAL = frozenset(
    {
        EnrollmentState.REPLIED,
        EnrollmentState.COMPLETED,
        EnrollmentState.STOPPED_SUPPRESSED,
        EnrollmentState.STOPPED_BOUNCED,
        EnrollmentState.STOPPED_MANUAL,
        EnrollmentState.STOPPED_IDENTITY_UNAVAILABLE,
    }
)
_ENROLLMENT_TRANSITIONS: dict[EnrollmentState, frozenset[EnrollmentState]] = {
    EnrollmentState.ENROLLED: frozenset(
        {EnrollmentState.IN_SEQUENCE, *_ENROLLMENT_TERMINAL}
    ),
    EnrollmentState.IN_SEQUENCE: frozenset(
        {EnrollmentState.IN_SEQUENCE, *_ENROLLMENT_TERMINAL}
    ),
    **{state: frozenset() for state in _ENROLLMENT_TERMINAL},
}


_ATTEMPT_TRANSITIONS: dict[MessageAttemptState, frozenset[MessageAttemptState]] = {
    MessageAttemptState.RESERVED: frozenset({MessageAttemptState.SENDING}),
    MessageAttemptState.SENDING: frozenset(
        {
            MessageAttemptState.SENT,
            MessageAttemptState.FAILED_TRANSIENT,
            MessageAttemptState.FAILED_PERMANENT,
        }
    ),
    MessageAttemptState.FAILED_TRANSIENT: frozenset(
        {
            MessageAttemptState.SENDING,
            MessageAttemptState.FAILED_TRANSIENT,
            MessageAttemptState.FAILED_PERMANENT,
        }
    ),
    MessageAttemptState.SENT: frozenset(),
    MessageAttemptState.FAILED_PERMANENT: frozenset(),
}


def _invalid_transition(source: Enum, target: Enum, allowed: frozenset[Enum]) -> None:
    choices = ",".join(sorted(item.value for item in allowed)) or "none"
    raise InvalidStateTransition(
        f"非法状态转换: {source.value} -> {target.value}; allowed={choices}"
    )


def validate_campaign_transition(source: CampaignState, target: CampaignState) -> None:
    if not isinstance(source, CampaignState) or not isinstance(target, CampaignState):
        raise InvalidStateTransition("Campaign 状态类型无效")
    allowed = _CAMPAIGN_TRANSITIONS[source]
    if target not in allowed:
        _invalid_transition(source, target, allowed)


def validate_enrollment_transition(
    source: EnrollmentState, target: EnrollmentState
) -> None:
    if not isinstance(source, EnrollmentState) or not isinstance(target, EnrollmentState):
        raise InvalidStateTransition("Enrollment 状态类型无效")
    allowed = _ENROLLMENT_TRANSITIONS[source]
    if target not in allowed:
        _invalid_transition(source, target, allowed)


def validate_message_attempt_transition(
    source: MessageAttemptState, target: MessageAttemptState
) -> None:
    if not isinstance(source, MessageAttemptState) or not isinstance(
        target, MessageAttemptState
    ):
        raise InvalidStateTransition("Message Attempt 状态类型无效")
    allowed = _ATTEMPT_TRANSITIONS[source]
    if target not in allowed:
        _invalid_transition(source, target, allowed)


@dataclass(frozen=True)
class SequenceStepSpec:
    step_number: int
    intent: StepIntent
    wait_days: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.step_number, int)
            or isinstance(self.step_number, bool)
            or self.step_number < 1
        ):
            raise ValidationError("step_number 无效")
        if not isinstance(self.intent, StepIntent):
            raise ValidationError("step_intent 无效")
        if (
            not isinstance(self.wait_days, int)
            or isinstance(self.wait_days, bool)
            or self.wait_days < 0
        ):
            raise ValidationError("wait_days 无效")


@dataclass(frozen=True)
class CampaignBoundary:
    markets: tuple[str, ...]
    target_entity_types: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    sender_identity_ids: tuple[SendingIdentityId, ...]
    steps: tuple[SequenceStepSpec, ...]
    daily_new_contact_limit: int
    daily_total_message_limit: int
    handoff_triggers: tuple[str, ...]
    stop_on_reply: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "markets",
            _freeze_texts(self.markets, field="markets", required=True),
        )
        object.__setattr__(
            self,
            "target_entity_types",
            _freeze_texts(
                self.target_entity_types,
                field="target_entity_types",
                required=True,
                lower=True,
            ),
        )
        object.__setattr__(
            self,
            "allowed_categories",
            _freeze_texts(
                self.allowed_categories,
                field="allowed_categories",
                required=True,
                lower=True,
            ),
        )
        if not isinstance(
            self.sender_identity_ids, (list, tuple, set, frozenset)
        ):
            raise ValidationError("sender_identity_ids 必须为集合")
        senders = tuple(sorted(set(self.sender_identity_ids)))
        if not senders:
            raise ValidationError("sender_identity_ids_required")
        for sender in senders:
            _require_id(sender, "sid", "sender_identity_id")
        object.__setattr__(self, "sender_identity_ids", senders)
        if not isinstance(self.steps, (list, tuple)):
            raise ValidationError("steps 必须为列表")
        steps = tuple(self.steps)
        if not steps:
            raise ValidationError("steps_required")
        if len(steps) > 5:
            raise ValidationError("steps_limit_exceeded")
        if any(not isinstance(step, SequenceStepSpec) for step in steps):
            raise ValidationError("step_shape_invalid")
        if tuple(step.step_number for step in steps) != tuple(range(1, len(steps) + 1)):
            raise ValidationError("step_numbers_contiguous")
        if steps[0].intent is not StepIntent.DISCOVERY:
            raise ValidationError("first_step_discovery")
        if steps[0].wait_days != 0:
            raise ValidationError("first_wait_zero")
        if any(step.wait_days <= 0 for step in steps[1:]):
            raise ValidationError("later_wait_positive")
        object.__setattr__(self, "steps", steps)
        for field, value in (
            ("daily_new_contact_limit", self.daily_new_contact_limit),
            ("daily_total_message_limit", self.daily_total_message_limit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValidationError(f"{field}_positive")
        if self.daily_new_contact_limit > self.daily_total_message_limit:
            raise ValidationError("new_contact_limit_exceeds_total")
        if self.stop_on_reply is not True:
            raise ValidationError("stop_on_reply_required")
        triggers = _freeze_texts(
            self.handoff_triggers,
            field="handoff_triggers",
            required=False,
            lower=True,
        )
        if any(trigger not in _HANDOFF_TRIGGERS for trigger in triggers):
            raise ValidationError("handoff_trigger_invalid")
        object.__setattr__(self, "handoff_triggers", triggers)

    def validate(self) -> list[str]:
        """构造时已完成全量验证；保留显式只读兼容入口。"""
        return []


@dataclass(frozen=True)
class CampaignVersion:
    tenant_id: TenantId
    campaign_id: CampaignId
    version: int
    name: str
    boundary: CampaignBoundary
    created_by: EmployeeId
    created_at: datetime

    def __post_init__(self) -> None:
        _require_id(self.campaign_id, "cmp", "campaign_id")
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise ValidationError("tenant_id 无效")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValidationError("Campaign version 无效")
        if not isinstance(self.name, str) or not self.name.strip() or self.name != self.name.strip():
            raise ValidationError("Campaign 名称无效")
        if not isinstance(self.boundary, CampaignBoundary):
            raise ValidationError("Campaign boundary 无效")
        if not isinstance(self.created_by, str) or not self.created_by:
            raise ValidationError("created_by 无效")
        _require_utc(self.created_at, "created_at")

    def revise(
        self,
        *,
        name: str,
        boundary: CampaignBoundary,
        created_at: datetime,
    ) -> CampaignVersion:
        return CampaignVersion(
            tenant_id=self.tenant_id,
            campaign_id=self.campaign_id,
            version=self.version + 1,
            name=name,
            boundary=boundary,
            created_by=self.created_by,
            created_at=created_at,
        )


@dataclass
class Campaign:
    tenant_id: TenantId
    campaign_id: CampaignId
    state: CampaignState
    current_version: int
    created_by: EmployeeId
    created_at: datetime
    round_robin_cursor: int = -1
    approval_id: str | None = None
    approved_by: EmployeeId | None = None
    approved_at: datetime | None = None
    paused_reason: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.campaign_id, "cmp", "campaign_id")
        if not isinstance(self.state, CampaignState):
            raise ValidationError("Campaign state 无效")
        if not isinstance(self.current_version, int) or isinstance(self.current_version, bool) or self.current_version < 1:
            raise ValidationError("Campaign current_version 无效")
        if not isinstance(self.round_robin_cursor, int) or isinstance(self.round_robin_cursor, bool) or self.round_robin_cursor < -1:
            raise ValidationError("Campaign cursor 无效")
        _require_utc(self.created_at, "created_at")

    def transition_to(self, target: CampaignState) -> None:
        validate_campaign_transition(self.state, target)
        self.state = target


_STOP_REASON_BY_STATE: dict[EnrollmentState, EnrollmentStopReason | None] = {
    EnrollmentState.REPLIED: EnrollmentStopReason.REPLY,
    EnrollmentState.COMPLETED: None,
    EnrollmentState.STOPPED_SUPPRESSED: EnrollmentStopReason.SUPPRESSION,
    EnrollmentState.STOPPED_BOUNCED: EnrollmentStopReason.HARD_BOUNCE,
    EnrollmentState.STOPPED_MANUAL: EnrollmentStopReason.MANUAL,
    EnrollmentState.STOPPED_IDENTITY_UNAVAILABLE: EnrollmentStopReason.IDENTITY_UNAVAILABLE,
}


@dataclass
class Enrollment:
    tenant_id: TenantId
    enrollment_id: EnrollmentId
    campaign_id: CampaignId
    campaign_version: int
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId
    state: EnrollmentState
    current_step: int
    next_send_at: datetime | None
    enrolled_at: datetime
    stopped_at: datetime | None
    stop_reason: EnrollmentStopReason | None
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        _require_id(self.enrollment_id, "enr", "enrollment_id")
        _require_id(self.campaign_id, "cmp", "campaign_id")
        _require_id(self.account_id, "acc", "account_id")
        _require_id(self.contact_point_id, "cp", "contact_point_id")
        _require_id(self.sending_identity_id, "sid", "sending_identity_id")
        if not isinstance(self.campaign_version, int) or isinstance(self.campaign_version, bool) or self.campaign_version < 1:
            raise ValidationError("campaign_version 无效")
        if not isinstance(self.current_step, int) or isinstance(self.current_step, bool) or self.current_step < 0:
            raise ValidationError("current_step 无效")
        if not isinstance(self.state, EnrollmentState):
            raise ValidationError("Enrollment state 无效")
        _require_utc(self.enrolled_at, "enrolled_at")
        if self.next_send_at is not None:
            _require_utc(self.next_send_at, "next_send_at")
        expected_reason = _STOP_REASON_BY_STATE.get(self.state)
        if self.state in _ENROLLMENT_TERMINAL:
            if self.stopped_at is None:
                raise ValidationError("terminal enrollment 缺少 stopped_at")
            _require_utc(self.stopped_at, "stopped_at")
            if self.stop_reason is not expected_reason:
                raise ValidationError("terminal enrollment stop_reason 不匹配")
        elif self.stopped_at is not None or self.stop_reason is not None:
            raise ValidationError("active enrollment 不能带停止字段")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValidationError("idempotency_key 无效")

    def transition_to(
        self,
        target: EnrollmentState,
        *,
        at: datetime,
        reason: EnrollmentStopReason | None = None,
    ) -> None:
        validate_enrollment_transition(self.state, target)
        _require_utc(at, "transition time")
        expected = _STOP_REASON_BY_STATE.get(target)
        if target in _ENROLLMENT_TERMINAL and reason is not expected:
            raise ValidationError("Enrollment stop reason 不匹配")
        self.state = target
        if target in _ENROLLMENT_TERMINAL:
            self.stopped_at = at
            self.stop_reason = expected


@dataclass
class MessageAttempt:
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    message_id: MessageId
    campaign_id: CampaignId
    enrollment_id: EnrollmentId
    campaign_version: int
    step_number: int
    sending_identity_id: SendingIdentityId
    idempotency_key: IdempotencyKey
    state: MessageAttemptState
    provider_ref: str | None
    failure_category: SendFailureCategory | None
    created_at: datetime
    updated_at: datetime
    send_claimed_at: datetime | None = None
    deterministic_message_id: str | None = dataclass_field(default=None, repr=False)
    idempotency_header: str | None = dataclass_field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value, prefix, field in (
            (self.attempt_id, "mat", "attempt_id"),
            (self.message_id, "msg", "message_id"),
            (self.campaign_id, "cmp", "campaign_id"),
            (self.enrollment_id, "enr", "enrollment_id"),
            (self.sending_identity_id, "sid", "sending_identity_id"),
        ):
            _require_id(value, prefix, field)
        if not isinstance(self.state, MessageAttemptState):
            raise ValidationError("Message Attempt state 无效")
        if not isinstance(self.step_number, int) or isinstance(self.step_number, bool) or self.step_number < 1:
            raise ValidationError("step_number 无效")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.send_claimed_at is not None:
            _require_utc(self.send_claimed_at, "send_claimed_at")
        if (self.deterministic_message_id is None) != (
            self.idempotency_header is None
        ):
            raise ValidationError("Message Attempt delivery correlation 必须成对")
        if self.deterministic_message_id is not None:
            message_route, message_digest = _split_deterministic_message_id(
                self.deterministic_message_id
            )
            header_route, header_digest = _split_idempotency_header(
                self.idempotency_header
            )
            if (message_route, message_digest) != (header_route, header_digest):
                raise ValidationError("Message Attempt delivery correlation 不匹配")
        if self.state is MessageAttemptState.RESERVED:
            valid = (
                self.provider_ref is None
                and self.failure_category is None
                and self.send_claimed_at is None
            )
        elif self.state is MessageAttemptState.SENDING:
            valid = (
                self.provider_ref is None
                and self.failure_category is None
                and self.send_claimed_at is not None
            )
        elif self.state is MessageAttemptState.SENT:
            valid = (
                isinstance(self.provider_ref, str)
                and bool(self.provider_ref)
                and self.failure_category is None
            )
        elif self.state is MessageAttemptState.FAILED_TRANSIENT:
            valid = (
                self.provider_ref is None
                and self.failure_category
                in {
                    SendFailureCategory.RATE_LIMITED,
                    SendFailureCategory.PROVIDER_TRANSIENT,
                    SendFailureCategory.PROVIDER_AUTH_REQUIRED,
                }
            )
        else:
            valid = (
                self.provider_ref is None
                and self.failure_category
                in {
                    SendFailureCategory.PROVIDER_PERMANENT,
                    SendFailureCategory.IDENTITY_UNAVAILABLE,
                }
            )
        if not valid:
            raise ValidationError("Message Attempt 状态字段不匹配")

    def bind_delivery_correlation(
        self, binding: DeliveryCorrelationBinding
    ) -> None:
        from domains.outreach.schemas import DeliveryCorrelationBinding

        if not isinstance(binding, DeliveryCorrelationBinding):
            raise ValidationError("delivery correlation binding 无效")
        current = (self.deterministic_message_id, self.idempotency_header)
        desired = (
            binding.deterministic_message_id,
            binding.idempotency_header,
        )
        if current == desired:
            return
        if current != (None, None):
            raise InvalidStateTransition("Message Attempt delivery correlation 不可改写")
        self.deterministic_message_id, self.idempotency_header = desired
        self.__post_init__()

    def transition_to(
        self,
        target: MessageAttemptState,
        *,
        at: datetime,
        provider_ref: str | None = None,
        failure_category: SendFailureCategory | None = None,
    ) -> None:
        validate_message_attempt_transition(self.state, target)
        _require_utc(at, "transition time")
        self.state = target
        if target is MessageAttemptState.SENDING:
            self.send_claimed_at = at
        self.provider_ref = provider_ref
        self.failure_category = failure_category
        self.updated_at = at
        self.__post_init__()


@dataclass(frozen=True)
class SuppressionEntry:
    tenant_id: TenantId
    suppression_id: SuppressionId
    target: SuppressionTarget
    reason: SuppressionReason
    occurred_at: datetime
    source_ref: str
    idempotency_key: IdempotencyKey
    created_at: datetime

    def __post_init__(self) -> None:
        _require_id(self.suppression_id, "sup", "suppression_id")
        if not isinstance(self.reason, SuppressionReason):
            raise ValidationError("suppression reason 无效")
        if not hasattr(self.target, "canonical_id") or not hasattr(self.target, "scope"):
            raise ValidationError("suppression target 无效")
        _require_utc(self.occurred_at, "occurred_at")
        _require_utc(self.created_at, "created_at")
        if not isinstance(self.source_ref, str) or not self.source_ref:
            raise ValidationError("source_ref 无效")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValidationError("idempotency_key 无效")


@dataclass(frozen=True)
class DailyQuotaUsage:
    tenant_id: TenantId
    campaign_id: CampaignId
    on_day: date
    new_contacts_reserved: int
    messages_reserved: int

    def __post_init__(self) -> None:
        _require_id(self.campaign_id, "cmp", "campaign_id")
        if not isinstance(self.on_day, date) or isinstance(self.on_day, datetime):
            raise ValidationError("on_day 必须是 date")
        for value in (self.new_contacts_reserved, self.messages_reserved):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValidationError("quota counter 无效")


@dataclass(frozen=True)
class ActionRecord:
    tenant_id: TenantId
    action_id: str
    action_key: str
    action: str
    entity_id: str
    actor_id: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        for field, value in (
            ("action_id", self.action_id),
            ("action_key", self.action_key),
            ("action", self.action),
            ("entity_id", self.entity_id),
            ("actor_id", self.actor_id),
        ):
            if not isinstance(value, str) or not value or any(character.isspace() for character in value):
                raise ValidationError(f"{field} 无效")
        _require_utc(self.occurred_at, "occurred_at")
