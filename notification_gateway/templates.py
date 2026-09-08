"""只从 typed context 渲染固定中文通知模板。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlsplit

from notification_gateway.jobs import (
    NotificationContext,
    NotificationJobClaim,
    NotificationKind,
)
from notification_gateway.models import Notification, NotificationPriority
from notification_gateway.quote_results import (
    NEXT_STEP,
    OUTCOMES,
    SOURCE_EVENT,
    TITLE,
    quote_result_link,
    valid_quote_result_recipient,
)
from shared.errors import PolicyViolation, ValidationError

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_JOB_ID = re.compile(rf"njb_{_ULID}\Z")
_TENANT_ID = re.compile(rf"tn_{_ULID}\Z")
_EMPLOYEE_ID = re.compile(rf"emp_{_ULID}\Z")
_CLAIM_TOKEN = re.compile(rf"njc_{_ULID}\Z")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
_SECRET_MARKER = re.compile(
    r"(?:bearer|token|secret|password|authorization|akia|sk-)", re.IGNORECASE
)
_SOURCE_EVENTS: dict[NotificationKind, frozenset[str]] = {
    NotificationKind.HANDOFF_ESCALATION: frozenset(
        {"HandoffRequested", "HandoffEscalationNotice"}
    ),
    NotificationKind.HANDOFF_QUEUE_BACKLOGGED: frozenset({"HandoffQueueBacklogged"}),
    NotificationKind.SENDING_IDENTITY_SUSPENDED: frozenset(
        {"SendingIdentitySuspended"}
    ),
    NotificationKind.REPUTATION_THRESHOLD_BREACHED: frozenset(
        {"ReputationThresholdBreached"}
    ),
    NotificationKind.COMMITMENT_OVERDUE: frozenset({"CommitmentOverdue"}),
    NotificationKind.APPROVAL_DECIDED: frozenset({"ApprovalDecided"}),
    NotificationKind.QUOTE_APPROVAL_RESULT: frozenset({SOURCE_EVENT}),
}
_KIND_PRIORITIES: dict[NotificationKind, NotificationPriority] = {
    NotificationKind.HANDOFF_ESCALATION: NotificationPriority.URGENT,
    NotificationKind.HANDOFF_QUEUE_BACKLOGGED: NotificationPriority.URGENT,
    NotificationKind.SENDING_IDENTITY_SUSPENDED: NotificationPriority.URGENT,
    NotificationKind.REPUTATION_THRESHOLD_BREACHED: NotificationPriority.NORMAL,
    NotificationKind.COMMITMENT_OVERDUE: NotificationPriority.URGENT,
    NotificationKind.APPROVAL_DECIDED: NotificationPriority.NORMAL,
    NotificationKind.QUOTE_APPROVAL_RESULT: NotificationPriority.LOW,
}
_HANDOFF_REASONS = frozenset(
    {"owner", "manager", "boss", "boss_reminder", "owner_pending", "owner_reminder"}
)
_APPROVAL_DECISIONS = frozenset({"approved", "rejected"})
_SUSPENSION_REASONS = frozenset(
    {
        "authentication_regression",
        "hard_bounce_rate",
        "complaint_rate",
        "spam_trap",
        "blocklisted",
    }
)
_REPUTATION_METRICS = frozenset(
    {"hard_bounce_rate", "complaint_rate", "spam_trap", "blocklisted"}
)
_REPUTATION_SEVERITIES = frozenset({"watch", "throttled", "suspended"})
_KIND_IDS: dict[NotificationKind, tuple[re.Pattern[str], re.Pattern[str] | None]] = {
    NotificationKind.QUOTE_APPROVAL_RESULT: (
        re.compile(rf"quo_{_ULID}\Z"),
        re.compile(rf"run_{_ULID}\Z"),
    ),
    NotificationKind.HANDOFF_ESCALATION: (
        re.compile(rf"hand_{_ULID}\Z"),
        re.compile(rf"opp_{_ULID}\Z"),
    ),
    NotificationKind.HANDOFF_QUEUE_BACKLOGGED: (
        re.compile(r"handoff_queue\Z"),
        None,
    ),
    NotificationKind.SENDING_IDENTITY_SUSPENDED: (
        re.compile(rf"sid_{_ULID}\Z"),
        None,
    ),
    NotificationKind.REPUTATION_THRESHOLD_BREACHED: (
        re.compile(rf"sid_{_ULID}\Z"),
        None,
    ),
    NotificationKind.COMMITMENT_OVERDUE: (
        re.compile(rf"com_{_ULID}\Z"),
        None,
    ),
    NotificationKind.APPROVAL_DECIDED: (
        re.compile(rf"apr_{_ULID}\Z"),
        re.compile(rf"emp_{_ULID}\Z"),
    ),
}


@dataclass(frozen=True)
class _Template:
    title: str
    next_step: str
    link: Callable[[NotificationContext], str]


_TEMPLATES: dict[NotificationKind, _Template] = {
    NotificationKind.QUOTE_APPROVAL_RESULT: _Template(
        TITLE,
        NEXT_STEP,
        lambda context: quote_result_link(context.primary_id),
    ),
    NotificationKind.HANDOFF_ESCALATION: _Template(
        "人工接管提醒",
        "处理人工接管任务",
        lambda context: f"/crm/handoffs/{context.primary_id}",
    ),
    NotificationKind.HANDOFF_QUEUE_BACKLOGGED: _Template(
        "接管队列积压",
        "查看并分配待接管任务",
        lambda _context: "/crm/handoffs",
    ),
    NotificationKind.SENDING_IDENTITY_SUSPENDED: _Template(
        "发件身份已熔断",
        "检查信誉指标并人工处理",
        lambda context: f"/crm/sending-identities/{context.primary_id}",
    ),
    NotificationKind.REPUTATION_THRESHOLD_BREACHED: _Template(
        "发件信誉指标预警",
        "检查发件身份信誉趋势",
        lambda context: f"/crm/sending-identities/{context.primary_id}",
    ),
    NotificationKind.COMMITMENT_OVERDUE: _Template(
        "承诺已逾期",
        "处理逾期承诺并更新下一步",
        lambda context: f"/crm/commitments/{context.primary_id}",
    ),
    NotificationKind.APPROVAL_DECIDED: _Template(
        "审批已有结果",
        "查看审批结果并继续处理",
        lambda context: f"/crm/approvals/{context.primary_id}",
    ),
}


class NotificationRenderer(Protocol):
    def render(self, claim: NotificationJobClaim) -> Notification: ...


class FixedNotificationTemplateRenderer:
    def render(self, claim: NotificationJobClaim) -> Notification:
        """校验 claim 与必填关联 ID 后，渲染固定模板。"""
        if not isinstance(claim, NotificationJobClaim):
            raise ValidationError("通知任务无法渲染")
        try:
            context = claim.context
        except AttributeError:
            raise ValidationError("通知任务无法渲染")
        if not isinstance(context, NotificationContext):
            raise ValidationError("通知任务无法渲染")
        if not _valid_claim(claim) or not _valid_context(context):
            raise ValidationError("通知任务无法渲染")
        template = _TEMPLATES.get(context.kind)
        if template is None or not _required_context_present(context):
            raise ValidationError("通知任务无法渲染")
        if (
            context.kind is NotificationKind.HANDOFF_ESCALATION
            and context.reason_code in {"owner_pending", "owner_reminder"}
        ):
            template = _Template(
                "待接管提醒",
                "查看接管资料并点击接受；接受后停止提醒",
                lambda context: f"/crm/handoffs/{context.primary_id}",
            )
        link = _validated_relative_link(template.link(context))
        return Notification(
            claim.tenant_id,
            claim.recipient,
            claim.priority,
            template.title,
            context,
            claim.source_event,
            claim.dedup_key,
            next_step=template.next_step,
            link=link,
            source_job_id=claim.job_id,
        )


def _required_context_present(context: NotificationContext) -> bool:
    if context.kind in {
        NotificationKind.HANDOFF_ESCALATION,
        NotificationKind.APPROVAL_DECIDED,
        NotificationKind.QUOTE_APPROVAL_RESULT,
    }:
        return context.secondary_id is not None
    return True


def _valid_claim(claim: NotificationJobClaim) -> bool:
    try:
        return _claim_fields_valid(claim)
    except AttributeError:
        return False


def _claim_fields_valid(claim: NotificationJobClaim) -> bool:
    context = claim.context
    id_patterns = _KIND_IDS.get(context.kind)
    if id_patterns is None:
        return False
    primary, secondary = id_patterns
    secondary_valid = (
        context.secondary_id is None
        if secondary is None
        else isinstance(context.secondary_id, str)
        and secondary.fullmatch(context.secondary_id) is not None
    )
    return (
        isinstance(claim.job_id, str)
        and _JOB_ID.fullmatch(claim.job_id) is not None
        and isinstance(claim.tenant_id, str)
        and _TENANT_ID.fullmatch(claim.tenant_id) is not None
        and isinstance(claim.recipient, str)
        and (
            valid_quote_result_recipient(claim.recipient)
            if context.kind is NotificationKind.QUOTE_APPROVAL_RESULT
            else _EMPLOYEE_ID.fullmatch(claim.recipient) is not None
        )
        and claim.priority is _KIND_PRIORITIES.get(context.kind)
        and isinstance(claim.source_event, str)
        and claim.source_event in _SOURCE_EVENTS.get(context.kind, frozenset())
        and isinstance(claim.dedup_key, str)
        and _safe_value(claim.dedup_key)
        and isinstance(claim.claim_token, str)
        and _CLAIM_TOKEN.fullmatch(claim.claim_token) is not None
        and type(claim.attempt_count) is int
        and claim.attempt_count >= 1
        and isinstance(context.primary_id, str)
        and primary.fullmatch(context.primary_id) is not None
        and secondary_valid
        and _valid_kind_context(context)
    )


def _safe_value(value: str) -> bool:
    return (
        _SAFE_VALUE.fullmatch(value) is not None
        and _SECRET_MARKER.search(value) is None
    )


def _valid_kind_context(context: NotificationContext) -> bool:
    if context.kind is NotificationKind.QUOTE_APPROVAL_RESULT:
        return context.reason_code in OUTCOMES and context.level is None
    if context.kind is NotificationKind.HANDOFF_ESCALATION:
        return (
            context.reason_code is None or context.reason_code in _HANDOFF_REASONS
        ) and context.level is None
    if context.kind is NotificationKind.HANDOFF_QUEUE_BACKLOGGED:
        return context.reason_code is None and context.level is not None
    if context.kind is NotificationKind.SENDING_IDENTITY_SUSPENDED:
        return context.reason_code in _SUSPENSION_REASONS and context.level is None
    if context.kind is NotificationKind.REPUTATION_THRESHOLD_BREACHED:
        if not isinstance(context.reason_code, str) or context.level is not None:
            return False
        parts = context.reason_code.split(":")
        return (
            len(parts) == 2
            and parts[0] in _REPUTATION_METRICS
            and parts[1] in _REPUTATION_SEVERITIES
        )
    if context.kind is NotificationKind.COMMITMENT_OVERDUE:
        return context.reason_code is None and context.level is None
    if context.kind is NotificationKind.APPROVAL_DECIDED:
        return context.reason_code in _APPROVAL_DECISIONS and context.level is None
    return False


def _valid_context(context: NotificationContext) -> bool:
    try:
        NotificationContext(
            context.kind,
            context.primary_id,
            context.secondary_id,
            context.reason_code,
            context.level,
        )
    except (AttributeError, ValidationError):
        return False
    return True


def _validated_relative_link(link: str) -> str:
    try:
        parsed = urlsplit(link)
    except (TypeError, ValueError):
        raise PolicyViolation("通知模板只接受相对深链") from None
    normalized = unquote(parsed.path)
    if (
        not isinstance(link, str)
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or normalized.startswith("//")
        or "\\" in link
        or "\\" in normalized
        or any(ord(char) < 32 or ord(char) == 127 for char in link)
    ):
        raise PolicyViolation("通知模板只接受相对深链")
    return link
