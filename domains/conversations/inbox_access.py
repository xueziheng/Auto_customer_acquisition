"""收件箱自身的权限与窄事务事实契约；请求范围仅为上界。"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId


class InboxAction(str, Enum):
    LIST = "inbox:list"
    READ = "inbox:read"
    CORRECT = "inbox:correct_classification"
    EVIDENCE_READ = "inbox:evidence_read"
    NEXT_QUESTIONS = "inbox:next_questions"


class InboxScope(str, Enum):
    SELF = "self"
    MANAGER = "manager"
    TENANT = "tenant"


@dataclass(frozen=True)
class InboxActor:
    tenant_id: TenantId
    employee_id: EmployeeId
    role: str
    scope: InboxScope
    allowed_owner_ids: frozenset[EmployeeId] = frozenset()


@dataclass(frozen=True)
class InboxEmployeeFacts:
    employee_id: EmployeeId
    role: str
    is_active: bool
    manager_id: EmployeeId | None


class InboxAccessFactsReader(Protocol):
    """实现绑定本次Conversations UoW事务，只投影当前员工及归属事实。"""

    async def read_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> InboxEmployeeFacts | None: ...

    async def read_owner(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> EmployeeId | None: ...

    async def lock_account_access(
        self, tenant_id: TenantId, account_id: ProspectAccountId, actor_id: EmployeeId
    ) -> None:
        """先锁ownership，再按ID升序锁actor和当前owner；保持到UoW退出。"""
        ...


def require_actor(tenant_id: TenantId, actor: InboxActor, action: InboxAction) -> None:
    """拒绝缺失/非法角色scope；调用方不能把owner集合提升为tenant。"""
    if (
        not isinstance(actor, InboxActor)
        or actor.tenant_id != tenant_id
        or not isinstance(action, InboxAction)
        or not isinstance(actor.allowed_owner_ids, frozenset)
        or {
            "boss": InboxScope.TENANT,
            "manager": InboxScope.MANAGER,
            "sales": InboxScope.SELF,
        }.get(actor.role)
        is not actor.scope
        or (
            actor.scope is InboxScope.SELF
            and actor.allowed_owner_ids != frozenset({actor.employee_id})
        )
        or (actor.scope is InboxScope.MANAGER and not actor.allowed_owner_ids)
    ):
        raise PermissionDenied("收件箱访问拒绝")


async def require_current_access(
    facts: InboxAccessFactsReader,
    tenant_id: TenantId,
    actor: InboxActor,
    action: InboxAction,
    account_id: ProspectAccountId | None = None,
) -> None:
    """snapshot ∩ 当前事实；角色变化拒绝，未分配只向当前boss开放。"""
    require_actor(tenant_id, actor, action)
    current = await facts.read_employee(tenant_id, actor.employee_id)
    if current is None or not current.is_active or current.role != actor.role:
        raise PermissionDenied("收件箱访问拒绝")
    if account_id is None or actor.scope is InboxScope.TENANT:
        return
    owner_id = await facts.read_owner(tenant_id, account_id)
    if owner_id is None or owner_id not in actor.allowed_owner_ids:
        raise PermissionDenied("收件箱访问拒绝")
    owner = await facts.read_employee(tenant_id, owner_id)
    if (
        owner is None
        or not owner.is_active
        or not (
            owner.employee_id == actor.employee_id
            or (
                actor.scope is InboxScope.MANAGER
                and owner.manager_id == actor.employee_id
            )
        )
    ):
        raise PermissionDenied("收件箱访问拒绝")
