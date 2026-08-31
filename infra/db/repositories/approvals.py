"""审批域 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, or_, select, text, tuple_
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.approvals.errors import ConflictingDecisionError
from domains.approvals.models import (
    ApprovalPackage,
    ApprovalState,
    ApprovalType,
    BlastRadius,
)
from domains.approvals.repository import ApprovalRepository
from infra.db.tables import ApprovalApplicationRow, ApprovalPackageRow
from shared.errors import (
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import ApprovalId, EmployeeId, RunId, TenantId

_logger = logging.getLogger("infra.db.repositories.approvals")


class _TenantBound:
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


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"审批持久化字段 {field} 无效")
    return cast(Mapping[str, object], value)


def _strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"审批持久化字段 {field} 无效")
    return cast(list[str], value)


def _blast_to_json(value: BlastRadius) -> dict[str, object]:
    return {
        "affected_entities": list(value.affected_entities),
        "if_approved": value.if_approved,
        "if_rejected": value.if_rejected,
        "reversible": value.reversible,
    }


def _blast_from_json(value: object) -> BlastRadius:
    item = _mapping(value, "blast_radius")
    if set(item) != {"affected_entities", "if_approved", "if_rejected", "reversible"}:
        raise ValidationError("审批影响范围持久化字段无效")
    if (
        not isinstance(item["if_approved"], str)
        or not isinstance(item["if_rejected"], str)
        or type(item["reversible"]) is not bool
    ):
        raise ValidationError("审批影响范围持久化字段无效")
    return BlastRadius(
        affected_entities=_strings(item["affected_entities"], "affected_entities"),
        if_approved=cast(str, item["if_approved"]),
        if_rejected=cast(str, item["if_rejected"]),
        reversible=cast(bool, item["reversible"]),
    )


def _row_to_package(row: ApprovalPackageRow) -> ApprovalPackage:
    try:
        approval_type = ApprovalType(row.approval_type)
        state = ApprovalState(row.state)
    except ValueError as exc:
        raise ValidationError("审批枚举持久化字段无效") from exc
    proposed_change = _mapping(row.proposed_change, "proposed_change")
    return ApprovalPackage(
        approval_id=ApprovalId(row.approval_id),
        tenant_id=TenantId(row.tenant_id),
        approval_type=approval_type,
        title=row.title,
        proposed_change=dict(proposed_change),
        reason=row.reason,
        blast_radius=_blast_from_json(row.blast_radius),
        created_at=row.created_at,
        expires_at=row.expires_at,
        state=state,
        proposed_by_run=RunId(row.proposed_by_run) if row.proposed_by_run else None,
        proposed_by_employee=(
            EmployeeId(row.proposed_by_employee) if row.proposed_by_employee else None
        ),
        evidence_refs=_strings(row.evidence_refs, "evidence_refs"),
        change_set_ref=row.change_set_ref,
        owner_employee=EmployeeId(row.owner_employee) if row.owner_employee else None,
        decided_at=row.decided_at,
        decided_by=EmployeeId(row.decided_by) if row.decided_by else None,
        decision_note=row.decision_note,
        applied_at=row.applied_at,
        apply_error=row.apply_error,
        contract_namespace=row.contract_namespace,
        request_hash=row.request_hash,
        expires_at_limit=row.expires_at_limit,
    )


class ApprovalRepositoryImpl(_TenantBound, ApprovalRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(session, tenant_id)
        self._now = now or (lambda: datetime.now(UTC))

    async def add(self, package: ApprovalPackage) -> None:
        self._require_tenant(package.tenant_id, "approval_add")
        self._session.add(
            ApprovalPackageRow(
                tenant_id=str(package.tenant_id),
                approval_id=str(package.approval_id),
                approval_type=package.approval_type.value,
                title=package.title,
                proposed_change=dict(package.proposed_change),
                reason=package.reason,
                blast_radius=_blast_to_json(package.blast_radius),
                created_at=package.created_at,
                expires_at=package.expires_at,
                state=package.state.value,
                proposed_by_run=(
                    str(package.proposed_by_run) if package.proposed_by_run else None
                ),
                proposed_by_employee=(
                    str(package.proposed_by_employee)
                    if package.proposed_by_employee
                    else None
                ),
                evidence_refs=list(package.evidence_refs),
                change_set_ref=package.change_set_ref,
                owner_employee=(
                    str(package.owner_employee) if package.owner_employee else None
                ),
                decided_at=package.decided_at,
                decided_by=str(package.decided_by) if package.decided_by else None,
                decision_note=package.decision_note,
                applied_at=package.applied_at,
                apply_error=package.apply_error,
                contract_namespace=package.contract_namespace,
                request_hash=package.request_hash,
                expires_at_limit=package.expires_at_limit,
            )
        )
        await self._session.flush()

    async def get(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> ApprovalPackage | None:
        return await self._get(tenant_id, approval_id, for_update=False)

    async def get_for_update(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> ApprovalPackage | None:
        return await self._get(tenant_id, approval_id, for_update=True)

    async def _get(
        self, tenant_id: TenantId, approval_id: ApprovalId, *, for_update: bool
    ) -> ApprovalPackage | None:
        self._require_tenant(tenant_id, "approval_get")
        statement = select(ApprovalPackageRow).where(
            ApprovalPackageRow.tenant_id == str(self._tenant_id),
            ApprovalPackageRow.approval_id == str(approval_id),
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _row_to_package(row) if row is not None else None

    async def update(self, package: ApprovalPackage) -> None:
        self._require_tenant(package.tenant_id, "approval_update")
        result = await self._session.execute(
            sa_update(ApprovalPackageRow)
            .where(
                ApprovalPackageRow.tenant_id == str(self._tenant_id),
                ApprovalPackageRow.approval_id == str(package.approval_id),
            )
            .values(
                state=package.state.value,
                decided_at=package.decided_at,
                decided_by=str(package.decided_by) if package.decided_by else None,
                decision_note=package.decision_note,
                applied_at=package.applied_at,
                apply_error=package.apply_error,
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise InvalidStateTransition("审批状态已变化")

    async def find_pending_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalPackage | None:
        self._require_tenant(tenant_id, "approval_find_pending_change_set")
        row = (
            await self._session.execute(
                select(ApprovalPackageRow).where(
                    ApprovalPackageRow.tenant_id == str(self._tenant_id),
                    ApprovalPackageRow.change_set_ref == change_set_ref,
                    ApprovalPackageRow.state == ApprovalState.PENDING.value,
                )
            )
        ).scalar_one_or_none()
        return _row_to_package(row) if row is not None else None

    async def lock_quote_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> None:
        """全状态首次提交串行，锁名与旧pending-only路径分离。"""
        self._require_tenant(tenant_id, "approval_quote_lock")
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"approval-quote-submit-v1:{tenant_id}:{change_set_ref}"},
        )

    async def find_quote_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalPackage | None:
        """数据库唯一索引兜底，不捕获冲突冒认成功。"""
        self._require_tenant(tenant_id, "approval_quote_find")
        row = (
            await self._session.execute(
                select(ApprovalPackageRow).where(
                    ApprovalPackageRow.tenant_id == tenant_id,
                    ApprovalPackageRow.change_set_ref == change_set_ref,
                    ApprovalPackageRow.contract_namespace == "quote-approval-v1",
                )
            )
        ).scalar_one_or_none()
        return _row_to_package(row) if row is not None else None

    async def list_quote_pending_candidates(
        self,
        tenant_id: TenantId,
        *,
        scan_started_at: datetime,
        after: tuple[datetime, ApprovalId] | None,
        limit: int,
    ) -> tuple[ApprovalPackage, ...]:
        """固定扫描时点与到期/ID游标，不能先排除本人起草/负责。"""
        self._require_tenant(tenant_id, "approval_quote_candidates")
        stmt = select(ApprovalPackageRow).where(
            ApprovalPackageRow.tenant_id == tenant_id,
            ApprovalPackageRow.contract_namespace == "quote-approval-v1",
            ApprovalPackageRow.state == "pending",
            ApprovalPackageRow.created_at <= scan_started_at,
        )
        if after is not None:
            stmt = stmt.where(
                tuple_(ApprovalPackageRow.expires_at, ApprovalPackageRow.approval_id)
                > after
            )
        rows = (
            await self._session.execute(
                stmt.order_by(
                    ApprovalPackageRow.expires_at, ApprovalPackageRow.approval_id
                ).limit(limit)
            )
        ).scalars()
        return tuple(_row_to_package(row) for row in rows)

    async def find_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalPackage | None:
        self._require_tenant(tenant_id, "approval_find_change_set")
        row = (
            await self._session.execute(
                select(ApprovalPackageRow)
                .where(
                    ApprovalPackageRow.tenant_id == str(self._tenant_id),
                    ApprovalPackageRow.change_set_ref == change_set_ref,
                )
                .order_by(
                    ApprovalPackageRow.created_at.desc(),
                    ApprovalPackageRow.approval_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return _row_to_package(row) if row is not None else None

    async def list_pending_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int, *, legacy_only: bool = False
    ) -> list[ApprovalPackage]:
        self._require_tenant(tenant_id, "approval_list_pending")
        employee = str(employee_id)
        statement = select(ApprovalPackageRow).where(
            ApprovalPackageRow.tenant_id == str(self._tenant_id),
            ApprovalPackageRow.state == ApprovalState.PENDING.value,
            or_(
                ApprovalPackageRow.proposed_by_employee.is_(None),
                ApprovalPackageRow.proposed_by_employee != employee,
            ),
            or_(
                ApprovalPackageRow.owner_employee.is_(None),
                ApprovalPackageRow.owner_employee != employee,
            ),
        )
        if legacy_only:
            statement = statement.where(ApprovalPackageRow.contract_namespace.is_(None))
        rows = (
            (
                await self._session.execute(
                    statement
                    .order_by(
                        ApprovalPackageRow.expires_at, ApprovalPackageRow.approval_id
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_package(row) for row in rows]

    async def list_expired_candidates(
        self, tenant_id: TenantId, now: datetime, limit: int
    ) -> list[ApprovalPackage]:
        self._require_tenant(tenant_id, "approval_list_expired")
        rows = (
            (
                await self._session.execute(
                    select(ApprovalPackageRow)
                    .where(
                        ApprovalPackageRow.tenant_id == str(self._tenant_id),
                        ApprovalPackageRow.state == ApprovalState.PENDING.value,
                        ApprovalPackageRow.expires_at <= now,
                    )
                    .order_by(
                        ApprovalPackageRow.expires_at, ApprovalPackageRow.approval_id
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_package(row) for row in rows]

    async def record_application(
        self, tenant_id: TenantId, approval_id: ApprovalId, idempotency_key: str
    ) -> bool:
        self._require_tenant(tenant_id, "approval_record_application")
        result = await self._session.execute(
            pg_insert(ApprovalApplicationRow)
            .values(
                tenant_id=str(self._tenant_id),
                approval_id=str(approval_id),
                idempotency_key=idempotency_key,
                created_at=self._now(),
            )
            .on_conflict_do_nothing(
                index_elements=(
                    ApprovalApplicationRow.tenant_id,
                    ApprovalApplicationRow.approval_id,
                )
            )
        )
        if cast(CursorResult[object], result).rowcount == 1:
            return True
        existing = await self._session.scalar(
            select(ApprovalApplicationRow.idempotency_key).where(
                ApprovalApplicationRow.tenant_id == str(self._tenant_id),
                ApprovalApplicationRow.approval_id == str(approval_id),
            )
        )
        if existing != idempotency_key:
            raise ConflictingDecisionError("审批应用幂等键冲突")
        return False

    async def count_by_state(self, tenant_id: TenantId, state: ApprovalState) -> int:
        self._require_tenant(tenant_id, "approval_count_state")
        value = await self._session.scalar(
            select(func.count())
            .select_from(ApprovalPackageRow)
            .where(
                ApprovalPackageRow.tenant_id == str(self._tenant_id),
                ApprovalPackageRow.state == state.value,
            )
        )
        return int(value or 0)


__all__ = ("ApprovalRepositoryImpl",)
