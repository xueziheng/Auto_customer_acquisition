"""ADR0028：同session安全元数据投影与域scope的SQL翻译，不编排员工业务。"""

from sqlalchemy import and_, exists, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from domains.conversations.inbox_access import (
    InboxAction,
    InboxActor,
    InboxEmployeeFacts,
    InboxScope,
    require_actor,
)
from infra.db.tables import ConversationRow, EmployeeRow, OwnershipLockRow
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId


class SqlAlchemyInboxAccessFactsReader:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session, self._tenant_id = session, tenant_id

    def _tenant(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise PermissionDenied("收件箱访问拒绝")

    async def read_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> InboxEmployeeFacts | None:
        self._tenant(tenant_id)
        row = (
            await self._session.execute(
                select(
                    EmployeeRow.employee_id,
                    EmployeeRow.role,
                    EmployeeRow.is_active,
                    EmployeeRow.manager_id,
                ).where(
                    EmployeeRow.tenant_id == tenant_id,
                    EmployeeRow.employee_id == employee_id,
                )
            )
        ).one_or_none()
        return (
            None
            if row is None
            else InboxEmployeeFacts(
                EmployeeId(row.employee_id),
                row.role,
                row.is_active,
                EmployeeId(row.manager_id) if row.manager_id else None,
            )
        )

    async def read_owner(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> EmployeeId | None:
        self._tenant(tenant_id)
        owner = (
            await self._session.execute(
                select(OwnershipLockRow.owner).where(
                    OwnershipLockRow.tenant_id == tenant_id,
                    OwnershipLockRow.account_id == account_id,
                )
            )
        ).scalar_one_or_none()
        return EmployeeId(owner) if owner else None

    async def lock_account_access(
        self, tenant_id: TenantId, account_id: ProspectAccountId, actor_id: EmployeeId
    ) -> None:
        self._tenant(tenant_id)
        owner = (
            await self._session.execute(
                select(OwnershipLockRow.owner)
                .where(
                    OwnershipLockRow.tenant_id == tenant_id,
                    OwnershipLockRow.account_id == account_id,
                )
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        ids = sorted({str(actor_id), *([owner] if owner else [])})
        await self._session.execute(
            select(EmployeeRow.employee_id)
            .where(
                EmployeeRow.tenant_id == tenant_id,
                EmployeeRow.employee_id.in_(ids),
            )
            .order_by(EmployeeRow.employee_id)
            .with_for_update(read=True)
        )


def inbox_predicate(tenant_id: TenantId, actor: InboxActor) -> ColumnElement[bool]:
    """单SQL语句读取当前事实，scope过滤先于LIMIT，不信任缓存owner。"""
    require_actor(tenant_id, actor, InboxAction.READ)
    principal = aliased(EmployeeRow)
    current = exists(
        select(principal.employee_id).where(
            principal.tenant_id == tenant_id,
            principal.employee_id == actor.employee_id,
            principal.is_active.is_(True),
            principal.role == actor.role,
        )
    )
    if actor.scope is InboxScope.TENANT:
        return current
    owner = aliased(EmployeeRow)
    owned = exists(
        select(OwnershipLockRow.account_id)
        .join(
            owner,
            and_(
                owner.tenant_id == tenant_id,
                owner.employee_id == OwnershipLockRow.owner,
            ),
        )
        .where(
            OwnershipLockRow.tenant_id == tenant_id,
            OwnershipLockRow.account_id == ConversationRow.account_id,
            owner.is_active.is_(True),
            owner.employee_id.in_(tuple(actor.allowed_owner_ids)),
            or_(
                owner.employee_id == actor.employee_id,
                (
                    owner.manager_id == actor.employee_id
                    if actor.scope is InboxScope.MANAGER
                    else false()
                ),
            ),
        )
    )
    return and_(current, owned)
