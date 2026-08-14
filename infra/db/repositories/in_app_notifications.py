"""站内通知的 tenant/recipient 双重过滤 PostgreSQL 仓储。"""

from __future__ import annotations

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import InAppNotificationRow
from notification_gateway.inbox import (
    InAppNotification,
    InAppNotificationView,
    NotificationCursor,
)
from notification_gateway.jobs import NotificationContext, NotificationKind
from notification_gateway.models import NotificationPriority
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, NotificationId, TenantId


class PostgresInAppNotificationStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def append(self, notification: InAppNotification) -> bool:
        async with self._factory() as session:
            result = await session.execute(insert(InAppNotificationRow).values(tenant_id=str(notification.tenant_id), notification_id=str(notification.notification_id), recipient_employee_id=str(notification.recipient), priority=notification.priority.value, title=notification.title, context_kind=notification.context.kind.value, primary_id=notification.context.primary_id, secondary_id=notification.context.secondary_id, reason_code=notification.context.reason_code, level=notification.context.level, relative_link=notification.relative_link, source_job_id=str(notification.source_job_id), created_at=notification.created_at).on_conflict_do_nothing(index_elements=["tenant_id", "source_job_id"]).returning(InAppNotificationRow.notification_id))
            created = result.scalar_one_or_none() is not None
            await session.commit()
            return created

    async def list_for_recipient(self, tenant_id: TenantId, recipient: EmployeeId, *, limit: int, before: NotificationCursor | None) -> tuple[InAppNotificationView, ...]:
        async with self._factory() as session:
            where = [InAppNotificationRow.tenant_id == str(tenant_id), InAppNotificationRow.recipient_employee_id == str(recipient)]
            if before is not None:
                where.append(or_(InAppNotificationRow.created_at < before.created_at, and_(InAppNotificationRow.created_at == before.created_at, InAppNotificationRow.notification_id < str(before.notification_id))))
            rows = (await session.execute(select(InAppNotificationRow).where(*where).order_by(InAppNotificationRow.created_at.desc(), InAppNotificationRow.notification_id.desc()).limit(limit))).scalars().all()
            return tuple(_view(row) for row in rows)

    async def mark_read(self, tenant_id: TenantId, recipient: EmployeeId, notification_id: NotificationId, *, read_at):
        async with self._factory() as session:
            changed = await session.execute(update(InAppNotificationRow).where(InAppNotificationRow.tenant_id == str(tenant_id), InAppNotificationRow.recipient_employee_id == str(recipient), InAppNotificationRow.notification_id == str(notification_id), InAppNotificationRow.read_at.is_(None)).values(read_at=read_at).returning(InAppNotificationRow))
            row = changed.scalar_one_or_none()
            if row is None:
                row = (await session.execute(select(InAppNotificationRow).where(InAppNotificationRow.tenant_id == str(tenant_id), InAppNotificationRow.recipient_employee_id == str(recipient), InAppNotificationRow.notification_id == str(notification_id)))).scalar_one_or_none()
            await session.commit()
            if row is None:
                raise ValidationError("通知不存在")
            return _view(row)


def _view(row: InAppNotificationRow) -> InAppNotificationView:
    return InAppNotificationView(NotificationId(row.notification_id), TenantId(row.tenant_id), NotificationPriority(row.priority), row.title, NotificationContext(NotificationKind(row.context_kind), row.primary_id, row.secondary_id, row.reason_code, row.level), row.relative_link, row.created_at, row.read_at)
