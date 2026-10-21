"""取得记录与真实客户档案、归属锁的同事务桥接。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.employees.permissions import (
    Actor,
    EmployeeScope,
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from domains.employees.service_impl import EmployeeServiceImpl
from domains.prospecting.schemas import AccountResolveRequest
from domains.prospecting.service import CandidateAllocation
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.prospecting_uow import BorrowedProspectingUnitOfWork
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.tables import (
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    ProspectAccountRow,
    ToolCallRow,
)
from shared.errors import IdempotencyConflict, PermissionDenied
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId


class _NoContactHasher:
    def fingerprint(self, canonical_value: str) -> str:
        raise PermissionDenied("分配桥接不能录入联系方式")


class CustomerAssignmentBridge:
    """只绑定账户与归属；公开邮箱不创建联系人或法律依据。"""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))

    async def transfer(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        allocation: CandidateAllocation,
        target: EmployeeId,
        acting: EmployeeId,
        reason: str,
    ) -> None:
        """在同一事务锁账户与归属；活动跟进须先由原负责人停止。"""
        account = (
            await session.scalars(
                select(ProspectAccountRow)
                .where(
                    ProspectAccountRow.tenant_id == tenant_id,
                    ProspectAccountRow.account_id == allocation.account_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        owner = (
            await session.scalars(
                select(OwnershipLockRow)
                .where(
                    OwnershipLockRow.tenant_id == tenant_id,
                    OwnershipLockRow.account_id == allocation.account_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if (
            allocation.tenant_id != tenant_id
            or account is None
            or owner is None
            or owner.owner != allocation.employee_id
        ):
            raise IdempotencyConflict("客户档案归属不一致，请刷新核对")
        # 客户guard先持归属行锁再入组，必须在其后取同一账户advisory锁。
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"{tenant_id}:account:{allocation.account_id}", 0
                    )
                )
            )
        )
        active = await session.scalar(
            select(OutreachEnrollmentRow.enrollment_id)
            .where(
                OutreachEnrollmentRow.tenant_id == tenant_id,
                OutreachEnrollmentRow.account_id == allocation.account_id,
                OutreachEnrollmentRow.state.in_(("enrolled", "in_sequence")),
            )
            .limit(1)
        )
        if active is not None:
            raise IdempotencyConflict("客户正在跟进，请先停止跟进再转交")
        unresolved = await session.scalar(
            select(OutreachMessageAttemptRow.attempt_id)
            .join(
                OutreachEnrollmentRow,
                and_(
                    OutreachEnrollmentRow.tenant_id == tenant_id,
                    OutreachEnrollmentRow.enrollment_id
                    == OutreachMessageAttemptRow.enrollment_id,
                ),
            )
            .outerjoin(
                ToolCallRow,
                and_(
                    ToolCallRow.tenant_id == tenant_id,
                    ToolCallRow.message_attempt_id
                    == OutreachMessageAttemptRow.attempt_id,
                    ToolCallRow.tool_id == "email.send",
                    ToolCallRow.idempotency_key.is_not(None),
                ),
            )
            .where(
                OutreachMessageAttemptRow.tenant_id == tenant_id,
                OutreachEnrollmentRow.account_id == allocation.account_id,
                or_(
                    OutreachMessageAttemptRow.state == "sending",
                    ToolCallRow.status.in_(
                        ("claimed", "executing", "failed_transient")
                    ),
                    ToolCallRow.error_category == "reconciliation_required",
                ),
            )
            .limit(1)
        )
        if unresolved is not None:
            raise IdempotencyConflict("客户仍有在途或待核对发送，请先核对再转交")
        employees = EmployeeRepositoryImpl(session, tenant_id)
        service = EmployeeServiceImpl(
            employees=employees,
            territories=TerritoryRepositoryImpl(session, tenant_id),
            ownership=OwnershipRepositoryImpl(session, tenant_id),
            now=lambda: max(self._now(), owner.locked_at + timedelta(microseconds=1)),
            manager_pool=lambda _: (),
            count_active_accounts=employees.count_active_accounts,
            authorizer=Phase1EmployeeAuthorizer(tenant_id),
            audit=StandardAuditLogger(),
        )
        current = await service.get_employee(
            tenant_id,
            acting,
            actor=Actor("system:assignment", EmployeeScope.SYSTEM, "system"),
        )
        if not current.is_active or current.role != "boss":
            raise PermissionDenied("转交身份已改变")
        await service.transfer(
            tenant_id,
            ProspectAccountId(account.account_id),
            target,
            actor=Actor(str(acting), EmployeeScope.TENANT, "boss"),
            transferred_by=acting,
            reason=reason,
        )

    async def bind(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        allocation: CandidateAllocation,
        target: EmployeeId,
        acting: EmployeeId,
    ) -> ProspectAccountId:
        """复用调用方已锁的事务；内层不得提交、回滚或关闭该 session。"""
        employees = EmployeeRepositoryImpl(session, tenant_id)
        employee_service = EmployeeServiceImpl(
            employees=employees,
            territories=TerritoryRepositoryImpl(session, tenant_id),
            ownership=OwnershipRepositoryImpl(session, tenant_id),
            now=self._now,
            manager_pool=lambda _: (),
            count_active_accounts=employees.count_active_accounts,
            authorizer=Phase1EmployeeAuthorizer(tenant_id),
            audit=StandardAuditLogger(),
        )
        current = await employee_service.get_employee(
            tenant_id,
            acting,
            actor=Actor("system:assignment", EmployeeScope.SYSTEM, "system"),
        )
        if (
            not current.is_active
            or current.role != "boss"
            or allocation.tenant_id != tenant_id
        ):
            raise PermissionDenied("分配身份已改变")
        prospecting = ProspectingServiceImpl(
            lambda requested: BorrowedProspectingUnitOfWork(session, requested),
            _NoContactHasher(),
            now=self._now,
        )
        candidate = allocation.candidate
        account_id = await prospecting.resolve_account(
            tenant_id,
            AccountResolveRequest(
                entity_name=candidate.name,
                country=candidate.country,
                website_domain=candidate.canonical_domain,
                field_provenance={
                    "name": candidate.provenance,
                    "country": candidate.provenance,
                },
            ),
        )
        owner = await employee_service.resolve_owner(
            tenant_id,
            account_id,
            actor=Actor(str(acting), EmployeeScope.TENANT, "boss"),
            country=candidate.country,
            boss_override=target,
        )
        if owner.owner != target:
            raise IdempotencyConflict("客户已有其他负责人")
        return account_id
