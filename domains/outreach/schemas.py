"""触达域公共 frozen DTO 与跨域安全快照。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from domains.outreach.models import (
    CampaignBoundary,
    CampaignState,
    DeliveryFeedbackKind,
    EnrollmentState,
    EnrollmentStopReason,
    MessageAttemptState,
    SendFailureCategory,
    SequenceStepSpec,
    StepIntent,
    SuppressionReason,
    SuppressionScope,
    _split_deterministic_message_id,
    _split_idempotency_header,
    _validate_delivery_route,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
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

_CREDENTIAL_MARKERS = ("bearer", "token", "secret", "password")
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_HANDOFF_TRIGGERS = frozenset(
    {
        "quantity_provided",
        "target_price_provided",
        "materials_requested",
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


def _require_safe_id(
    value: object,
    field: str,
    *,
    prefix: str | None = None,
) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValidationError(f"{field} 无效")
    lowered = value.lower()
    if (
        value != value.strip()
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in value
        )
        or "://" in value
        or any(marker in lowered for marker in _CREDENTIAL_MARKERS)
    ):
        raise ValidationError(f"{field} 无效")
    if prefix is not None and re.fullmatch(rf"{prefix}_{_ULID}", value) is None:
        raise ValidationError(f"{field} 无效")
    return value


def _require_safe_text(
    value: object,
    field: str,
    *,
    max_length: int = 200,
    allow_spaces: bool = False,
) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise ValidationError(f"{field} 无效")
    lowered = value.lower()
    if (
        value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or (not allow_spaces and any(character.isspace() for character in value))
        or "://" in value
        or "@" in value
        or any(marker in lowered for marker in _CREDENTIAL_MARKERS)
    ):
        raise ValidationError(f"{field} 无效")
    return value


def _freeze_texts(
    values: object,
    field: str,
    *,
    required: bool,
    lower: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValidationError(f"{field} 必须为集合")
    result: set[str] = set()
    for value in values:
        text = _require_safe_text(value, field, max_length=64)
        result.add(text.lower() if lower else text)
    if required and not result:
        raise ValidationError(f"{field} 不能为空")
    return tuple(sorted(result))


class ContactVerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    RISKY = "risky"
    INVALID = "invalid"


class ContactLegalBasis(str, Enum):
    LEGITIMATE_INTEREST = "legitimate_interest"
    CONSENT = "consent"
    EXISTING_CUSTOMER = "existing_customer"


class OutreachSenderRole(str, Enum):
    COLD_OUTREACH = "cold_outreach"
    PRIMARY_BUSINESS = "primary_business"
    TRANSACTIONAL = "transactional"


class CampaignApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ReplyState(str, Enum):
    NO_REPLY = "no_reply"
    REPLIED = "replied"


@dataclass(frozen=True)
class DeliveryCorrelationBinding:
    deterministic_message_id: str = field(repr=False)
    idempotency_header: str = field(repr=False)
    route_id: str

    def __post_init__(self) -> None:
        route_id = _validate_delivery_route(self.route_id)
        message_pair = _split_deterministic_message_id(
            self.deterministic_message_id
        )
        header_pair = _split_idempotency_header(self.idempotency_header)
        if message_pair != header_pair or message_pair[0] != route_id:
            raise ValidationError("delivery correlation binding 不匹配")


@dataclass(frozen=True)
class DeliveryCorrelationLookup:
    deterministic_message_id: str | None = field(default=None, repr=False)
    idempotency_header: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.deterministic_message_id is None and self.idempotency_header is None:
            raise ValidationError("delivery correlation lookup 缺少关联键")
        if self.deterministic_message_id is not None:
            _split_deterministic_message_id(self.deterministic_message_id)
        if self.idempotency_header is not None:
            _split_idempotency_header(self.idempotency_header)


@dataclass(frozen=True)
class DeliveryFeedbackTarget:
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId

    def __post_init__(self) -> None:
        for value, field_name, prefix in (
            (self.tenant_id, "tenant_id", "tn"),
            (self.attempt_id, "attempt_id", "mat"),
            (self.enrollment_id, "enrollment_id", "enr"),
            (self.account_id, "account_id", "acc"),
            (self.contact_point_id, "contact_point_id", "cp"),
            (self.sending_identity_id, "sending_identity_id", "sid"),
        ):
            _require_safe_id(value, field_name, prefix=prefix)


@dataclass(frozen=True)
class SequenceStepRequest:
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
            raise ValidationError("step intent 无效")
        if (
            not isinstance(self.wait_days, int)
            or isinstance(self.wait_days, bool)
            or self.wait_days < 0
        ):
            raise ValidationError("wait_days 无效")


@dataclass(frozen=True)
class CampaignCreateRequest:
    name: str
    markets: tuple[str, ...]
    target_entity_types: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    sender_identity_ids: tuple[SendingIdentityId, ...]
    steps: tuple[SequenceStepRequest, ...]
    daily_new_contact_limit: int
    daily_total_message_limit: int
    handoff_triggers: tuple[str, ...]
    stop_on_reply: bool = True

    def __post_init__(self) -> None:
        _require_safe_text(self.name, "Campaign name", allow_spaces=True)
        markets = _freeze_texts(self.markets, "markets", required=True)
        entity_types = _freeze_texts(
            self.target_entity_types,
            "target_entity_types",
            required=True,
            lower=True,
        )
        categories = _freeze_texts(
            self.allowed_categories,
            "allowed_categories",
            required=True,
            lower=True,
        )
        if not isinstance(
            self.sender_identity_ids, (list, tuple, set, frozenset)
        ):
            raise ValidationError("sender_identity_ids 必须为集合")
        for sender in self.sender_identity_ids:
            _require_safe_id(sender, "sender_identity_id", prefix="sid")
        senders = tuple(sorted(set(self.sender_identity_ids)))
        if not senders:
            raise ValidationError("sender_identity_ids 不能为空")
        if not isinstance(self.steps, (list, tuple)):
            raise ValidationError("steps 必须为列表")
        steps = tuple(self.steps)
        if any(not isinstance(step, SequenceStepRequest) for step in steps):
            raise ValidationError("steps 包含无效步骤")
        triggers = _freeze_texts(
            self.handoff_triggers,
            "handoff_triggers",
            required=False,
            lower=True,
        )
        if any(trigger not in _HANDOFF_TRIGGERS for trigger in triggers):
            raise ValidationError("handoff_trigger_invalid")
        boundary = CampaignBoundary(
            markets=markets,
            target_entity_types=entity_types,
            allowed_categories=categories,
            sender_identity_ids=senders,
            steps=tuple(
                SequenceStepSpec(step.step_number, step.intent, step.wait_days)
                for step in steps
            ),
            daily_new_contact_limit=self.daily_new_contact_limit,
            daily_total_message_limit=self.daily_total_message_limit,
            handoff_triggers=triggers,
            stop_on_reply=self.stop_on_reply,
        )
        object.__setattr__(self, "markets", boundary.markets)
        object.__setattr__(self, "target_entity_types", boundary.target_entity_types)
        object.__setattr__(self, "allowed_categories", boundary.allowed_categories)
        object.__setattr__(self, "sender_identity_ids", boundary.sender_identity_ids)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "handoff_triggers", boundary.handoff_triggers)


@dataclass(frozen=True)
class EnrollmentCreateRequest:
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        _require_safe_id(self.account_id, "account_id", prefix="acc")
        _require_safe_id(self.contact_point_id, "contact_point_id", prefix="cp")
        _require_safe_text(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True)
class SuppressionTarget:
    contact_point_id: ContactPointId | None = None
    account_id: ProspectAccountId | None = None

    def __post_init__(self) -> None:
        present = [
            value
            for value in (self.contact_point_id, self.account_id)
            if value is not None
        ]
        if len(present) != 1:
            raise ValidationError("抑制目标必须且只能指定一个资源")
        if self.contact_point_id is not None:
            _require_safe_id(
                self.contact_point_id, "contact_point_id", prefix="cp"
            )
        if self.account_id is not None:
            _require_safe_id(self.account_id, "account_id", prefix="acc")

    @property
    def scope(self) -> SuppressionScope:
        return (
            SuppressionScope.CONTACT
            if self.contact_point_id is not None
            else SuppressionScope.ACCOUNT
        )

    @property
    def canonical_id(self) -> str:
        value = self.contact_point_id or self.account_id
        assert value is not None
        return str(value)


@dataclass(frozen=True)
class SuppressionRequest:
    target: SuppressionTarget
    reason: SuppressionReason
    occurred_at: datetime
    source_ref: str
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        if not isinstance(self.target, SuppressionTarget):
            raise ValidationError("suppression target 无效")
        if not isinstance(self.reason, SuppressionReason):
            raise ValidationError("suppression reason 无效")
        _require_utc(self.occurred_at, "occurred_at")
        _require_safe_text(self.source_ref, "source_ref", max_length=64)
        _require_safe_text(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True)
class ContactEligibilitySnapshot:
    tenant_id: TenantId
    contact_point_id: ContactPointId
    account_id: ProspectAccountId
    verification: ContactVerificationStatus
    verified_at: datetime | None
    legal_basis: ContactLegalBasis
    legal_basis_ref: str
    contact_belongs_to_account: bool
    country: str
    entity_type: str
    qualified_categories: frozenset[str]
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_safe_id(self.tenant_id, "tenant_id", prefix="tn")
        _require_safe_id(self.contact_point_id, "contact_point_id", prefix="cp")
        _require_safe_id(self.account_id, "account_id", prefix="acc")
        if not isinstance(self.verification, ContactVerificationStatus):
            raise ValidationError("verification 无效")
        if not isinstance(self.legal_basis, ContactLegalBasis):
            raise ValidationError("legal_basis 无效")
        _require_safe_text(self.legal_basis_ref, "legal_basis_ref", max_length=64)
        if not isinstance(self.contact_belongs_to_account, bool):
            raise ValidationError("contact_belongs_to_account 无效")
        _require_safe_text(self.country, "country", max_length=64)
        _require_safe_text(self.entity_type, "entity_type", max_length=64)
        categories = frozenset(
            _freeze_texts(
                self.qualified_categories,
                "qualified_categories",
                required=False,
                lower=True,
            )
        )
        object.__setattr__(self, "qualified_categories", categories)
        _require_utc(self.observed_at, "observed_at")
        if self.verification is ContactVerificationStatus.VERIFIED:
            if self.verified_at is None:
                raise ValidationError("verified contact 缺少 verified_at")
            _require_utc(self.verified_at, "verified_at")
        elif self.verified_at is not None:
            raise ValidationError("未验证 contact 不能带 verified_at")


@dataclass(frozen=True)
class SendingIdentityEligibilitySnapshot:
    tenant_id: TenantId
    identity_id: SendingIdentityId
    role: OutreachSenderRole
    authentication_passed: bool
    sendable: bool
    remaining_slots: int
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_safe_id(self.tenant_id, "tenant_id", prefix="tn")
        _require_safe_id(self.identity_id, "identity_id", prefix="sid")
        if not isinstance(self.role, OutreachSenderRole):
            raise ValidationError("sender role 无效")
        if not isinstance(self.authentication_passed, bool) or not isinstance(
            self.sendable, bool
        ):
            raise ValidationError("sender eligibility bool 无效")
        if (
            not isinstance(self.remaining_slots, int)
            or isinstance(self.remaining_slots, bool)
            or self.remaining_slots < 0
        ):
            raise ValidationError("remaining_slots 无效")
        _require_utc(self.observed_at, "observed_at")


@dataclass(frozen=True)
class CampaignApprovalSnapshot:
    tenant_id: TenantId
    campaign_id: CampaignId
    version: int
    approval_id: ApprovalId
    state: CampaignApprovalState
    approved_by: EmployeeId | None
    approved_at: datetime | None

    def __post_init__(self) -> None:
        _require_safe_id(self.tenant_id, "tenant_id", prefix="tn")
        _require_safe_id(self.campaign_id, "campaign_id", prefix="cmp")
        _require_safe_id(self.approval_id, "approval_id", prefix="apr")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValidationError("approval version 无效")
        if not isinstance(self.state, CampaignApprovalState):
            raise ValidationError("approval state 无效")
        if self.state is CampaignApprovalState.APPROVED:
            _require_safe_id(self.approved_by, "approved_by", prefix="emp")
            _require_utc(self.approved_at, "approved_at")
        elif self.approved_by is not None or self.approved_at is not None:
            raise ValidationError("非 approved snapshot 不能带审批结果")


@dataclass(frozen=True)
class ReplyStatusSnapshot:
    tenant_id: TenantId
    contact_point_id: ContactPointId
    account_id: ProspectAccountId
    state: ReplyState
    replied_at: datetime | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_safe_id(self.tenant_id, "tenant_id", prefix="tn")
        _require_safe_id(self.contact_point_id, "contact_point_id", prefix="cp")
        _require_safe_id(self.account_id, "account_id", prefix="acc")
        if not isinstance(self.state, ReplyState):
            raise ValidationError("reply state 无效")
        _require_utc(self.observed_at, "observed_at")
        if self.state is ReplyState.REPLIED:
            _require_utc(self.replied_at, "replied_at")
        elif self.replied_at is not None:
            raise ValidationError("no-reply snapshot 不能带 replied_at")


@dataclass(frozen=True)
class CampaignBoundaryView:
    markets: tuple[str, ...]
    target_entity_types: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    sender_identity_ids: tuple[SendingIdentityId, ...]
    steps: tuple[SequenceStepRequest, ...]
    daily_new_contact_limit: int
    daily_total_message_limit: int
    handoff_triggers: tuple[str, ...]
    stop_on_reply: bool


@dataclass(frozen=True)
class CampaignView:
    tenant_id: TenantId
    campaign_id: CampaignId
    name: str
    state: CampaignState
    version: int
    boundary: CampaignBoundaryView
    approval_id: ApprovalId | None
    approved_by: EmployeeId | None
    approved_at: datetime | None
    paused_reason: str | None
    created_by: EmployeeId
    created_at: datetime
    today_new_contacts_reserved: int
    today_messages_reserved: int


@dataclass(frozen=True)
class EnrollmentView:
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


@dataclass(frozen=True)
class MessageAttemptView:
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
    deterministic_message_id: str | None = field(default=None, repr=False)
    idempotency_header: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class MessageSendPreflight:
    """Gateway 发送检查所需的最小安全资源绑定。"""

    tenant_id: TenantId
    attempt_id: MessageAttemptId
    campaign_id: CampaignId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId
    campaign_version: int
    step_number: int
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        for value, field_name, prefix in (
            (self.tenant_id, "tenant_id", "tn"),
            (self.attempt_id, "attempt_id", "mat"),
            (self.campaign_id, "campaign_id", "cmp"),
            (self.enrollment_id, "enrollment_id", "enr"),
            (self.account_id, "account_id", "acc"),
            (self.contact_point_id, "contact_point_id", "cp"),
            (self.sending_identity_id, "sending_identity_id", "sid"),
        ):
            _require_safe_id(value, field_name, prefix=prefix)
        for numeric_value, field_name in (
            (self.campaign_version, "campaign_version"),
            (self.step_number, "step_number"),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value < 1
            ):
                raise ValidationError(f"{field_name} 无效")
        _require_safe_text(self.idempotency_key, "idempotency_key", max_length=200)


@dataclass(frozen=True)
class SuppressionView:
    tenant_id: TenantId
    suppression_id: SuppressionId
    target: SuppressionTarget
    reason: SuppressionReason
    occurred_at: datetime
    source_ref: str
    idempotency_key: IdempotencyKey
    created_at: datetime


@dataclass(frozen=True)
class SuppressionResult:
    created: bool
    suppression: SuppressionView
    stopped_count: int


@dataclass(frozen=True)
class DraftContent:
    """工作流草稿内容：主题与正文（发送前二次检查仍以当前事实为准）。"""

    subject: str
    body: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subject, str)
            or not self.subject.strip()
            or len(self.subject) > 998
        ):
            raise ValidationError("草稿主题无效")
        if (
            not isinstance(self.body, str)
            or not self.body.strip()
            or len(self.body) > 100_000
        ):
            raise ValidationError("草稿正文无效")


class SendDenialReason(str, Enum):
    """``prepare_send`` 的固定拒绝分类；工作流据此分流，不猜原因文本。"""

    REPLY_RECEIVED = "reply_received"
    SUPPRESSED = "suppressed"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    QUOTA_EXHAUSTED = "quota_exhausted"
    CAMPAIGN_NOT_ACTIVE = "campaign_not_active"
    NOT_DUE = "not_due"
    ENROLLMENT_TERMINAL = "enrollment_terminal"


@dataclass(frozen=True)
class SendAuthorization:
    """已授权的一次序列发送：attempt 幂等键 + 工作流草稿内容。"""

    tenant_id: TenantId
    enrollment_id: EnrollmentId
    campaign_id: CampaignId
    campaign_version: int
    step_number: int
    sending_identity_id: SendingIdentityId
    attempt_id: MessageAttemptId
    idempotency_key: IdempotencyKey
    subject: str
    body: str


@dataclass(frozen=True)
class SendDecision:
    """``prepare_send`` 的 typed 结果：授权或固定分类拒绝，两者互斥。"""

    authorized: bool
    authorization: SendAuthorization | None
    denial_reason: SendDenialReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.authorized, bool):
            raise ValidationError("发送决策无效")
        if self.authorized and (
            self.authorization is None or self.denial_reason is not None
        ):
            raise ValidationError("发送决策无效")
        if not self.authorized and (
            self.authorization is not None or self.denial_reason is None
        ):
            raise ValidationError("发送决策无效")
