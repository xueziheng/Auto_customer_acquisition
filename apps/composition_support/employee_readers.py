"""每调用独立员工事务与公开员工事实映射；不共享运行实例。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.approvals.service import CatalogApprovalActorFact
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import AuditLogger as EmployeeAuditLogger
from domains.employees.permissions import EmployeeAuthorizer
from domains.employees.schemas import EmployeeView
from domains.employees.service import EmployeeService
from domains.employees.service_impl import EmployeeServiceImpl
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId


class EmployeeServiceScope(Protocol):
    def __call__(
        self, tenant_id: TenantId
    ) -> AbstractAsyncContextManager[EmployeeService]: ...


@asynccontextmanager
async def employee_service_scope(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    *,
    now: Callable[[], datetime],
    authorizer: EmployeeAuthorizer,
    audit: EmployeeAuditLogger,
) -> AsyncIterator[EmployeeService]:
    """为一次调用创建独立员工服务事务，异常回滚且总是关闭会话。"""
    session = factory()
    try:
        employees = EmployeeRepositoryImpl(session, tenant_id)
        yield EmployeeServiceImpl(
            employees=employees,
            territories=TerritoryRepositoryImpl(session, tenant_id),
            ownership=OwnershipRepositoryImpl(session, tenant_id),
            now=now,
            manager_pool=lambda _: (),
            count_active_accounts=employees.count_active_accounts,
            authorizer=authorizer,
            audit=audit,
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


class RequestScopedHandoffEmployeeReader:
    """让 workflow 的每次员工读取使用独立 service scope。"""

    def __init__(self, scope: EmployeeServiceScope) -> None:
        self._scope = scope

    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView:
        async with self._scope(tenant_id) as service:
            return await service.get_employee(tenant_id, employee_id, actor=actor)


class RequestScopedDirectiveEmployeeReader:
    """用员工域公开服务为指令域提供老板校验与展示名。"""

    def __init__(
        self,
        scope: EmployeeServiceScope,
        actor: EmployeeActor,
    ) -> None:
        self._scope = scope
        self._actor = actor

    async def is_active_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> bool:
        async with self._scope(tenant_id) as service:
            employees = await service.list_active(tenant_id, actor=self._actor)
        return any(
            employee.employee_id == employee_id and employee.role == "boss"
            for employee in employees
        )

    async def names_for(
        self, tenant_id: TenantId, employee_ids: tuple[EmployeeId, ...]
    ) -> dict[EmployeeId, str]:
        async with self._scope(tenant_id) as service:
            employees = await service.list_active(tenant_id, actor=self._actor)
        wanted = set(employee_ids)
        return {
            employee.employee_id: employee.name
            for employee in employees
            if employee.employee_id in wanted
        }


class RequestScopedCatalogApprovalActorReader:
    """每次联结读取都从员工服务重取当前 Catalog 内部读资格。"""

    _ROLES = frozenset({"boss", "product", "sourcing", "finance"})

    def __init__(
        self,
        scope: EmployeeServiceScope,
        actor: EmployeeActor,
    ) -> None:
        self._scope = scope
        self._actor = actor

    async def read_actor(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> CatalogApprovalActorFact:
        async with self._scope(tenant_id) as service:
            employee = await service.get_employee(
                tenant_id, employee_id, actor=self._actor
            )
        return CatalogApprovalActorFact.model_validate(
            {
                "tenant_id": employee.tenant_id,
                "employee_id": employee.employee_id,
                "current_role": employee.role,
                "active": employee.is_active,
                "eligible": employee.is_active and employee.role in self._ROLES,
            }
        )


class CurrentEmployeeUserReader:
    """按明确 UserId 找当前 active 员工，缺失/重复映射失败关闭。"""

    def __init__(self, scope: EmployeeServiceScope, actor: EmployeeActor) -> None:
        self._scope, self._actor = scope, actor

    async def get_for_user(self, tenant_id: TenantId, user_id: UserId) -> EmployeeView:
        async with self._scope(tenant_id) as service:
            employees = await service.list_active(tenant_id, actor=self._actor)
        matches = [
            e
            for e in employees
            if e.tenant_id == tenant_id and e.user_id == user_id and e.is_active
        ]
        if len(matches) != 1:
            raise ValidationError("当前员工身份不可用")
        return matches[0]
