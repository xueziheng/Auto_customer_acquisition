"""开发模式身份断言：只信员工域 public DTO，不信 role/scope header。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from domains.conversations.service import InboxActor, InboxScope
from domains.employees.errors import EmployeeNotFoundError
from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    EmployeeScope,
)
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
    ScopeLevel,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId

from .middleware import ApiSettings

if TYPE_CHECKING:
    from .dependencies import ConfiguredApiDependencies

_KNOWN_ROLES = frozenset(
    {"boss", "manager", "sales", "sourcing", "product", "finance", "viewer"}
)


@dataclass(frozen=True)
class RequestIdentity:
    """从 EmployeeView 确定性推导的两域 actor 与固定租户。"""

    tenant_id: TenantId
    employee: EmployeeView
    employee_actor: EmployeeActor
    opportunity_actor: OpportunityActor

    @property
    def conversation_inbox_actor(self) -> InboxActor:
        """沿同次public员工快照机械映射，执行时由Conversations再次求交。"""
        return InboxActor(
            self.tenant_id,
            self.employee.employee_id,
            self.employee.role,
            {
                "boss": InboxScope.TENANT,
                "manager": InboxScope.MANAGER,
                "sales": InboxScope.SELF,
            }.get(self.employee.role, InboxScope.SELF),
            self.opportunity_actor.scope.allowed_owners or frozenset(),
        )


def _single_header(request: Request, name: bytes) -> list[str]:
    """返回 ASGI 原始头值列表；重复值由调用方失败关闭。"""
    return [
        value.decode("latin-1")
        for header_name, value in request.scope.get("headers", [])
        if header_name.lower() == name
    ]


def _validate_employee_view(
    employee: EmployeeView,
    *,
    expected_tenant: TenantId,
    expected_employee: EmployeeId,
) -> None:
    """校验 public DTO 与断言完全一致；拒绝时不带任何实体值。"""
    if (
        employee.tenant_id != expected_tenant
        or employee.employee_id != expected_employee
        or not employee.is_active
        or employee.role not in _KNOWN_ROLES
    ):
        raise PermissionDenied("员工身份不可用")


def _employee_actor(employee: EmployeeView) -> EmployeeActor:
    """为员工域生成显式最小 scope；非 CRM 角色仍由第一道 role gate 拒绝。"""
    scopes = {
        "sales": EmployeeScope.SELF,
        "manager": EmployeeScope.MANAGER,
        "boss": EmployeeScope.TENANT,
    }
    return EmployeeActor(
        actor_id=str(employee.employee_id),
        scope=scopes.get(employee.role, EmployeeScope.SELF),
        role=employee.role,
    )


def _opportunity_actor(
    employee: EmployeeView, active_employees: list[EmployeeView]
) -> OpportunityActor:
    """由角色和 public 下属 DTO 生成不可变机会域 ABAC scope。"""
    if employee.role == "sales":
        scope = OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({employee.employee_id}),
        )
    elif employee.role == "manager":
        direct_reports = {
            candidate.employee_id
            for candidate in active_employees
            if candidate.tenant_id == employee.tenant_id
            and candidate.is_active
            and candidate.manager_id == employee.employee_id
        }
        scope = OpportunityScope(
            level=ScopeLevel.MANAGER,
            allowed_owners=frozenset({employee.employee_id, *direct_reports}),
        )
    elif employee.role == "boss":
        scope = OpportunityScope(level=ScopeLevel.TENANT)
    else:
        scope = OpportunityScope()
    return OpportunityActor(
        actor_id=str(employee.employee_id), scope=scope, role=employee.role
    )


async def resolve_request_identity(
    request: Request,
    settings: ApiSettings,
    dependencies: ConfiguredApiDependencies,
) -> RequestIdentity:
    """在同一个 employee service request scope 内完成查询与身份推导。"""
    if not settings.dev_mode:
        raise HTTPException(status_code=403)

    header_values = _single_header(request, b"x-employee-id")
    if not header_values:
        raise HTTPException(status_code=401)
    if len(header_values) != 1 or not header_values[0] or not header_values[0].strip():
        raise PermissionDenied("员工身份不可用")

    tenant_id = settings.tenant
    employee_id = EmployeeId(header_values[0])
    try:
        async with dependencies.employees(tenant_id) as service:
            employee = await service.get_employee(
                tenant_id,
                employee_id,
                actor=dependencies.employee_lookup_actor,
            )
            _validate_employee_view(
                employee,
                expected_tenant=tenant_id,
                expected_employee=employee_id,
            )
            active_employees = (
                await service.list_active(
                    tenant_id, actor=dependencies.employee_lookup_actor
                )
                if employee.role == "manager"
                else []
            )
    except EmployeeNotFoundError:
        raise PermissionDenied("员工身份不可用") from None

    return RequestIdentity(
        tenant_id=tenant_id,
        employee=employee,
        employee_actor=_employee_actor(employee),
        opportunity_actor=_opportunity_actor(employee, active_employees),
    )
