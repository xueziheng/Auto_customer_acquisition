"""只从 typed context 渲染固定中文通知模板。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlsplit

from notification_gateway.jobs import (
    NotificationContext,
    NotificationJobClaim,
    NotificationKind,
)
from notification_gateway.models import Notification
from shared.errors import PolicyViolation, ValidationError


@dataclass(frozen=True)
class _Template:
    title: str
    next_step: str
    link: Callable[[NotificationContext], str]


_TEMPLATES: dict[NotificationKind, _Template] = {
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
        if not isinstance(claim, NotificationJobClaim) or not isinstance(
            claim.context, NotificationContext
        ):
            raise ValidationError("通知任务无法渲染")
        context = claim.context
        if not _valid_context(context):
            raise ValidationError("通知任务无法渲染")
        template = _TEMPLATES.get(context.kind)
        if template is None or not _required_context_present(context):
            raise ValidationError("通知任务无法渲染")
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
    }:
        return context.secondary_id is not None
    return True


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
