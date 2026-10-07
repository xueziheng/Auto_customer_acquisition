"""承诺账本 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.commitments.models import (
    Commitment,
    CommitmentStatus,
    CommitmentType,
)
from domains.commitments.repository import CommitmentRepository
from infra.db.tables import CommitmentRow
from shared.errors import InvalidStateTransition, TenantIsolationViolation
from shared.schemas.identifiers import (
    CommitmentId,
    EmployeeId,
    MessageId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
)

_logger = logging.getLogger("security.tenant_isolation")


def _to_model(row: CommitmentRow) -> Commitment:
    return Commitment(
        commitment_id=CommitmentId(row.commitment_id),
        tenant_id=TenantId(row.tenant_id),
        commitment_type=CommitmentType(row.commitment_type),
        owner=EmployeeId(row.owner),
        action=row.action,
        due_at=row.due_at,
        source_message_id=MessageId(row.source_message_id),
        verbatim=row.verbatim,
        created_at=row.created_at,
        status=CommitmentStatus(row.status),
        due_at_uncertain=row.due_at_uncertain,
        account_id=ProspectAccountId(row.account_id) if row.account_id else None,
        opportunity_id=(
            OpportunityId(row.opportunity_id) if row.opportunity_id else None
        ),
        extracted_by=row.extracted_by,
        confirmed_by=EmployeeId(row.confirmed_by) if row.confirmed_by else None,
        confirmed_at=row.confirmed_at,
        fulfilled_at=row.fulfilled_at,
        escalated_at=row.escalated_at,
    )


def _values(commitment: Commitment) -> dict[str, object]:
    return {
        "tenant_id": str(commitment.tenant_id),
        "commitment_id": str(commitment.commitment_id),
        "commitment_type": commitment.commitment_type.value,
        "owner": str(commitment.owner),
        "action": commitment.action,
        "due_at": commitment.due_at,
        "due_at_uncertain": commitment.due_at_uncertain,
        "source_message_id": str(commitment.source_message_id),
        "verbatim": commitment.verbatim,
        "status": commitment.status.value,
        "account_id": str(commitment.account_id) if commitment.account_id else None,
        "opportunity_id": (
            str(commitment.opportunity_id) if commitment.opportunity_id else None
        ),
        "extracted_by": commitment.extracted_by,
        "confirmed_by": (
            str(commitment.confirmed_by) if commitment.confirmed_by else None
        ),
        "confirmed_at": commitment.confirmed_at,
        "created_at": commitment.created_at,
        "fulfilled_at": commitment.fulfilled_at,
        "escalated_at": commitment.escalated_at,
    }


class CommitmentRepositoryImpl(CommitmentRepository):
    """所有查询显式绑定构造时的租户，跨租户调用直接拒绝。"""

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")

    async def add(self, commitment: Commitment) -> None:
        self._require_tenant(commitment.tenant_id, "commitment_add")
        self._session.add(CommitmentRow(**_values(commitment)))
        await self._session.flush()

    async def add_if_absent(
        self, commitment: Commitment
    ) -> tuple[Commitment, bool]:
        self._require_tenant(commitment.tenant_id, "commitment_add_if_absent")
        created = (
            await self._session.execute(
                pg_insert(CommitmentRow)
                .values(**_values(commitment))
                .on_conflict_do_nothing(constraint="uq_commitments_source_action")
                .returning(CommitmentRow.commitment_id)
            )
        ).scalar_one_or_none()
        if created is not None:
            return commitment, True
        winner = await self.find_duplicate(
            commitment.tenant_id,
            str(commitment.source_message_id),
            commitment.action,
        )
        if winner is None:
            raise RuntimeError("承诺去重 winner 缺失")
        return winner, False

    async def get(
        self, tenant_id: TenantId, commitment_id: CommitmentId
    ) -> Commitment | None:
        return await self._get(tenant_id, commitment_id, for_update=False)

    async def get_for_update(
        self, tenant_id: TenantId, commitment_id: CommitmentId
    ) -> Commitment | None:
        return await self._get(tenant_id, commitment_id, for_update=True)

    async def _get(
        self,
        tenant_id: TenantId,
        commitment_id: CommitmentId,
        *,
        for_update: bool,
    ) -> Commitment | None:
        self._require_tenant(tenant_id, "commitment_get")
        statement = select(CommitmentRow).where(
            CommitmentRow.tenant_id == str(self._tenant_id),
            CommitmentRow.commitment_id == str(commitment_id),
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _to_model(row) if row is not None else None

    async def update(self, commitment: Commitment) -> None:
        self._require_tenant(commitment.tenant_id, "commitment_update")
        values = _values(commitment)
        values.pop("tenant_id")
        values.pop("commitment_id")
        result = await self._session.execute(
            sa_update(CommitmentRow)
            .where(
                CommitmentRow.tenant_id == str(self._tenant_id),
                CommitmentRow.commitment_id == str(commitment.commitment_id),
            )
            .values(**values)
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise InvalidStateTransition("承诺状态已变化")

    async def find_duplicate(
        self, tenant_id: TenantId, source_message_id: str, action: str
    ) -> Commitment | None:
        self._require_tenant(tenant_id, "commitment_find_duplicate")
        row = (
            await self._session.execute(
                select(CommitmentRow).where(
                    CommitmentRow.tenant_id == str(self._tenant_id),
                    CommitmentRow.source_message_id == source_message_id,
                    CommitmentRow.action == action,
                )
            )
        ).scalar_one_or_none()
        return _to_model(row) if row is not None else None

    async def list_due(
        self, tenant_id: TenantId, before: datetime, limit: int
    ) -> list[Commitment]:
        self._require_tenant(tenant_id, "commitment_list_due")
        rows = (
            (
                await self._session.execute(
                    select(CommitmentRow)
                    .where(
                        CommitmentRow.tenant_id == str(self._tenant_id),
                        CommitmentRow.confirmed_by.is_not(None),
                        CommitmentRow.confirmed_at.is_not(None),
                        CommitmentRow.due_at_uncertain.is_(False),
                        CommitmentRow.status.in_(
                            (
                                CommitmentStatus.PENDING.value,
                                CommitmentStatus.WAITING_CUSTOMER.value,
                                CommitmentStatus.OVERDUE.value,
                            )
                        ),
                        CommitmentRow.due_at <= before,
                    )
                    .order_by(CommitmentRow.due_at, CommitmentRow.commitment_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        return [_to_model(row) for row in rows]

    async def list_for_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        statuses: list[CommitmentStatus],
    ) -> list[Commitment]:
        self._require_tenant(tenant_id, "commitment_list_employee")
        rows = (
            (
                await self._session.execute(
                    select(CommitmentRow)
                    .where(
                        CommitmentRow.tenant_id == str(self._tenant_id),
                        CommitmentRow.owner == str(employee_id),
                        CommitmentRow.status.in_([status.value for status in statuses]),
                    )
                    .order_by(CommitmentRow.due_at, CommitmentRow.commitment_id)
                )
            )
            .scalars()
            .all()
        )
        return [_to_model(row) for row in rows]


__all__ = ("CommitmentRepositoryImpl",)
