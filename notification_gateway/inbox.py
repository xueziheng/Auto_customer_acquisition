"""站内通知收件箱的访问与只读视图契约。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from notification_gateway.jobs import NotificationContext
from notification_gateway.models import NotificationPriority
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    NotificationId,
    NotificationJobId,
    TenantId,
)


@dataclass(frozen=True, repr=False)
class NotificationCursor:
    created_at: datetime
    notification_id: NotificationId

    def __post_init__(self) -> None:
        if not _is_utc(self.created_at):
            raise ValidationError("通知游标无效")


@dataclass(frozen=True)
class InAppNotification:
    notification_id: NotificationId
    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    title: str
    context: NotificationContext
    relative_link: str | None
    source_job_id: NotificationJobId
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.priority, NotificationPriority) or not isinstance(self.context, NotificationContext) or not _is_utc(self.created_at):
            raise ValidationError("站内通知无效")


@dataclass(frozen=True)
class InAppNotificationView:
    notification_id: NotificationId
    tenant_id: TenantId
    priority: NotificationPriority
    title: str
    context: NotificationContext
    relative_link: str | None
    created_at: datetime
    read_at: datetime | None


@dataclass(frozen=True)
class InboxActor:
    tenant_id: TenantId
    employee_id: EmployeeId


class InAppNotificationService(Protocol):
    async def list_notifications(self, tenant_id: TenantId, *, actor: InboxActor, limit: int, before: NotificationCursor | None) -> tuple[InAppNotificationView, ...]: ...
    async def mark_read(self, tenant_id: TenantId, notification_id: NotificationId, *, actor: InboxActor) -> InAppNotificationView: ...


class InAppNotificationStore(Protocol):
    async def append(self, notification: InAppNotification) -> bool: ...
    async def list_for_recipient(self, tenant_id: TenantId, recipient: EmployeeId, *, limit: int, before: NotificationCursor | None) -> tuple[InAppNotificationView, ...]: ...
    async def mark_read(self, tenant_id: TenantId, recipient: EmployeeId, notification_id: NotificationId, *, read_at: datetime) -> InAppNotificationView: ...


class InAppNotificationServiceImpl:
    def __init__(self, store: InAppNotificationStore, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    async def list_notifications(self, tenant_id: TenantId, *, actor: InboxActor, limit: int, before: NotificationCursor | None) -> tuple[InAppNotificationView, ...]:
        self._authorize(tenant_id, actor)
        _validate_limit(limit)
        return await self._store.list_for_recipient(tenant_id, actor.employee_id, limit=limit, before=before)

    async def mark_read(self, tenant_id: TenantId, notification_id: NotificationId, *, actor: InboxActor) -> InAppNotificationView:
        self._authorize(tenant_id, actor)
        now = self._now()
        if not _is_utc(now):
            raise ValidationError("通知时间无效")
        return await self._store.mark_read(tenant_id, actor.employee_id, notification_id, read_at=now)

    @staticmethod
    def _authorize(tenant_id: TenantId, actor: InboxActor) -> None:
        if tenant_id != actor.tenant_id:
            raise TenantIsolationViolation("跨租户通知访问被拒绝")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("通知分页大小无效")


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None and value.utcoffset() == UTC.utcoffset(value)
