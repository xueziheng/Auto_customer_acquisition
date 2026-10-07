"""通知任务的 tenant-scoped PostgreSQL 仓储。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import NotificationJobRow
from notification_gateway.jobs import (
    NotificationContext,
    NotificationJob,
    NotificationJobClaim,
    NotificationJobStatus,
)
from shared.schemas.identifiers import EmployeeId, NotificationJobId, TenantId, new_id


class PostgresNotificationJobStore:
    """每个方法独立提交，确保 Outbox handler 返回前任务已 durable。"""

    _LEASE = timedelta(seconds=300)

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, now: Callable[[], datetime] | None = None) -> None:
        self._factory = factory
        self._now = now or (lambda: datetime.now(UTC))

    async def enqueue(self, job: NotificationJob) -> bool:
        async with self._factory() as session:
            result = await session.execute(insert(NotificationJobRow).values(tenant_id=str(job.tenant_id), notification_job_id=str(job.job_id), source_event_fingerprint=job.source_event_fingerprint, source_event=job.source_event, recipient_employee_id=str(job.recipient), priority=job.priority.value, context_kind=job.context.kind.value, primary_id=job.context.primary_id, secondary_id=job.context.secondary_id, reason_code=job.context.reason_code, level=job.context.level, dedup_key=job.dedup_key, status=NotificationJobStatus.PENDING.value, available_at=job.created_at, attempt_count=0, created_at=job.created_at).on_conflict_do_nothing(index_elements=["tenant_id", "source_event_fingerprint", "recipient_employee_id", "context_kind"]).returning(NotificationJobRow.notification_job_id))
            created = result.scalar_one_or_none() is not None
            await session.commit()
            return created

    async def claim_due(self, tenant_id: TenantId, *, limit: int, lease_owner: str) -> tuple[NotificationJobClaim, ...]:
        now = self._now()
        async with self._factory() as session:
            rows = (await session.execute(select(NotificationJobRow).where(NotificationJobRow.tenant_id == str(tenant_id), NotificationJobRow.status.in_([NotificationJobStatus.PENDING.value, NotificationJobStatus.PROCESSING.value]), NotificationJobRow.available_at <= now, or_(NotificationJobRow.lease_expires_at.is_(None), NotificationJobRow.lease_expires_at <= now)).order_by(NotificationJobRow.available_at, NotificationJobRow.notification_job_id).limit(limit).with_for_update(skip_locked=True))).scalars().all()
            claims: list[NotificationJobClaim] = []
            for row in rows:
                token = new_id("njc")
                row.status = NotificationJobStatus.PROCESSING.value
                row.lease_owner = lease_owner
                row.lease_token = token
                row.lease_expires_at = now + self._LEASE
                row.attempt_count += 1
                claims.append(_claim(row, token))
            await session.commit()
            return tuple(claims)

    async def complete(self, tenant_id: TenantId, job_id: NotificationJobId, *, claim_token: str) -> bool:
        return await self._finish(tenant_id, job_id, claim_token, NotificationJobStatus.COMPLETED, None)

    async def retry(self, tenant_id: TenantId, job_id: NotificationJobId, *, claim_token: str, error: BaseException) -> bool:
        now = self._now()
        async with self._factory() as session:
            row = (await session.execute(select(NotificationJobRow).where(NotificationJobRow.tenant_id == str(tenant_id), NotificationJobRow.notification_job_id == str(job_id), NotificationJobRow.status == NotificationJobStatus.PROCESSING.value, NotificationJobRow.lease_token == claim_token).with_for_update())).scalar_one_or_none()
            if row is None:
                await session.commit()
                return False
            row.status = NotificationJobStatus.PENDING.value
            row.available_at = now + timedelta(seconds=min(30 * 2 ** (row.attempt_count - 1), 3600))
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            row.last_error = type(error).__name__
            await session.commit()
            return True

    async def reject(self, tenant_id: TenantId, job_id: NotificationJobId, *, claim_token: str, error: BaseException) -> bool:
        return await self._finish(tenant_id, job_id, claim_token, NotificationJobStatus.REJECTED, type(error).__name__)

    async def _finish(self, tenant_id: TenantId, job_id: NotificationJobId, claim_token: str, status: NotificationJobStatus, error: str | None) -> bool:
        async with self._factory() as session:
            result = await session.execute(update(NotificationJobRow).where(NotificationJobRow.tenant_id == str(tenant_id), NotificationJobRow.notification_job_id == str(job_id), NotificationJobRow.status == NotificationJobStatus.PROCESSING.value, NotificationJobRow.lease_token == claim_token).values(status=status.value, lease_owner=None, lease_token=None, lease_expires_at=None, completed_at=self._now(), last_error=error).returning(NotificationJobRow.notification_job_id))
            changed = result.scalar_one_or_none() is not None
            await session.commit()
            return changed


def _claim(row: NotificationJobRow, token: str) -> NotificationJobClaim:
    from notification_gateway.jobs import NotificationKind
    from notification_gateway.models import NotificationPriority

    return NotificationJobClaim(NotificationJobId(row.notification_job_id), TenantId(row.tenant_id), EmployeeId(row.recipient_employee_id), NotificationPriority(row.priority), NotificationContext(NotificationKind(row.context_kind), row.primary_id, row.secondary_id, row.reason_code, row.level), row.source_event, row.dedup_key, token, row.attempt_count)
