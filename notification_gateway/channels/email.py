"""内部事务邮件通知渠道；外部动作仍由 Tool Gateway 执行。"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

from notification_gateway.jobs import NotificationContext, NotificationKind
from notification_gateway.models import Notification, NotificationPriority
from shared.errors import PolicyViolation

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT = re.compile(rf"tn_{_ULID}\Z")
_EMPLOYEE = re.compile(rf"emp_{_ULID}\Z")
_JOB = re.compile(rf"njb_{_ULID}\Z")
_CREDENTIAL = re.compile(
    r"(?:bearer|token|secret|password|authorization|akia|sk-)", re.IGNORECASE
)


@runtime_checkable
class TransactionalNotificationSender(Protocol):
    async def send(self, notification: Notification) -> None: ...


class EmailNotificationChannel:
    name = "email"

    def __init__(self, sender: TransactionalNotificationSender) -> None:
        if not isinstance(sender, TransactionalNotificationSender):
            raise TypeError("事务通知 sender 无效")
        self._sender = sender

    async def deliver(self, notification: Notification) -> None:
        """在进入 Tool Gateway 前拒绝 forged/free-text 通知。"""
        if not _valid_notification(notification):
            raise PolicyViolation("事务邮件通知不满足安全边界")
        await self._sender.send(notification)


def _valid_notification(value: object) -> bool:
    if not isinstance(value, Notification):
        return False
    context = value.context
    strings = (
        value.title,
        value.next_step,
        value.link,
        value.source_event,
        value.dedup_key,
        context.primary_id if isinstance(context, NotificationContext) else None,
        context.secondary_id if isinstance(context, NotificationContext) else None,
        context.reason_code if isinstance(context, NotificationContext) else None,
    )
    return (
        isinstance(value.tenant_id, str)
        and _TENANT.fullmatch(value.tenant_id) is not None
        and isinstance(value.recipient, str)
        and _EMPLOYEE.fullmatch(value.recipient) is not None
        and isinstance(value.source_job_id, str)
        and _JOB.fullmatch(value.source_job_id) is not None
        and isinstance(value.priority, NotificationPriority)
        and isinstance(context, NotificationContext)
        and isinstance(context.kind, NotificationKind)
        and isinstance(value.title, str)
        and bool(value.title)
        and isinstance(value.next_step, str)
        and bool(value.next_step)
        and isinstance(value.link, str)
        and _safe_relative_link(value.link)
        and all(
            item is None
            or (
                isinstance(item, str)
                and _CREDENTIAL.search(item) is None
                and all(ord(char) >= 32 and ord(char) != 127 for char in item)
            )
            for item in strings
        )
    )


def _safe_relative_link(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        normalized = unquote(parsed.path)
    except ValueError:
        return False
    return not (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or normalized.startswith("//")
        or "\\" in value
        or "\\" in normalized
    )
