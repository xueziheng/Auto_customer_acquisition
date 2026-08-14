"""把 job-backed typed 通知追加到站内收件箱。"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from urllib.parse import unquote, urlsplit

from notification_gateway.inbox import InAppNotification, InAppNotificationStore
from notification_gateway.jobs import NotificationContext
from notification_gateway.models import Notification, NotificationPriority
from shared.errors import PolicyViolation, ValidationError
from shared.schemas.identifiers import NotificationId, NotificationJobId, new_id

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT_ID = re.compile(rf"tn_{_ULID}\Z")
_EMPLOYEE_ID = re.compile(rf"emp_{_ULID}\Z")
_JOB_ID = re.compile(rf"njb_{_ULID}\Z")
_CREDENTIAL_MARKER = re.compile(
    r"(?:bearer|token|secret|authorization|password)", re.IGNORECASE
)
_CREDENTIALS = (
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[psu]_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)password\s*=\s*['\"]?[^\s'\"`\\=,;，。；、：！？…（）【】《》]+"),
)


class InAppChannel:
    name = "in_app"

    def __init__(
        self,
        store: InAppNotificationStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self._store = store
        self._now = now
        self._id_factory = id_factory

    async def deliver(self, notification: Notification) -> None:
        """最终验证身份、上下文、source job、深链和凭证形态后追加。"""
        if not _valid_notification(notification):
            raise PolicyViolation("站内通知不满足安全边界")
        created_at = self._now()
        if not _is_utc(created_at):
            raise PolicyViolation("站内通知时间无效")
        await self._store.append(
            InAppNotification(
                NotificationId(self._id_factory("not")),
                notification.tenant_id,
                notification.recipient,
                notification.priority,
                notification.title,
                notification.context,
                notification.link,
                cast(NotificationJobId, notification.source_job_id),
                created_at,
            )
        )


def _valid_notification(notification: object) -> bool:
    if not isinstance(notification, Notification):
        return False
    values = (
        notification.title,
        notification.source_event,
        notification.dedup_key,
        notification.next_step,
        notification.link,
        notification.context.primary_id
        if isinstance(notification.context, NotificationContext)
        else None,
        notification.context.secondary_id
        if isinstance(notification.context, NotificationContext)
        else None,
        notification.context.reason_code
        if isinstance(notification.context, NotificationContext)
        else None,
    )
    return (
        isinstance(notification.tenant_id, str)
        and _TENANT_ID.fullmatch(notification.tenant_id) is not None
        and isinstance(notification.recipient, str)
        and _EMPLOYEE_ID.fullmatch(notification.recipient) is not None
        and isinstance(notification.priority, NotificationPriority)
        and isinstance(notification.context, NotificationContext)
        and _valid_context(notification.context)
        and isinstance(notification.source_job_id, str)
        and _JOB_ID.fullmatch(notification.source_job_id) is not None
        and isinstance(notification.link, str)
        and _valid_relative_link(notification.link)
        and all(
            value is None
            or (
                isinstance(value, str)
                and not _credential_shaped(value)
            )
            for value in values
        )
    )


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


def _valid_relative_link(link: str) -> bool:
    try:
        parsed = urlsplit(link)
    except ValueError:
        return False
    normalized = unquote(parsed.path)
    return not (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or normalized.startswith("//")
        or "\\" in link
        or "\\" in normalized
        or any(ord(char) < 32 or ord(char) == 127 for char in link)
    )


def _credential_shaped(value: str) -> bool:
    decoded = value
    while True:
        normalized = unquote(decoded)
        if normalized == decoded:
            break
        decoded = normalized
    return any(
        pattern.search(candidate) is not None
        for candidate in (value, decoded)
        for pattern in (_CREDENTIAL_MARKER, *_CREDENTIALS)
    )


def _is_utc(value: datetime) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
        and value.utcoffset() == UTC.utcoffset(value)
    )
