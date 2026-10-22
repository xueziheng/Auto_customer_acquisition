"""Team & Territory：活跃员工与确定性分配矩阵查询。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from domains.employees.permissions import EmployeeAction
from domains.employees.schemas import EmployeeView, TerritoryAssignmentView
from shared.errors import TransientError
from workflows.employee_accounts import EmployeeAccountCreation

from ..authentication import EmployeeAccountCreateRequest
from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    require_employee_action,
)
from ..identity import RequestIdentity

router = APIRouter()

_BOSS_ONLY = frozenset({"boss"})
_employee_list_gate = Depends(
    require_employee_action(
        EmployeeAction.EMPLOYEE_LIST,
        allowed_roles=_BOSS_ONLY,
    )
)
_territory_list_gate = Depends(
    require_employee_action(
        EmployeeAction.ASSIGNMENT_LIST,
        allowed_roles=_BOSS_ONLY,
    )
)
_account_create_gate = Depends(
    require_employee_action(
        EmployeeAction.ACCOUNT_CREATE,
        allowed_roles=_BOSS_ONLY,
    )
)


@router.get("/account-creation", response_model=bool)
async def account_creation_available(
    request: Request,
    identity: Annotated[RequestIdentity, _account_create_gate],
) -> bool:
    del identity
    return isinstance(
        getattr(request.app.state, "employee_accounts", None), EmployeeAccountCreation
    )


@router.post("/employees", response_model=EmployeeView, status_code=201)
async def create_team_employee(
    request: Request,
    command: EmployeeAccountCreateRequest,
    identity: Annotated[RequestIdentity, _account_create_gate],
) -> EmployeeView:
    service = getattr(request.app.state, "employee_accounts", None)
    if not isinstance(service, EmployeeAccountCreation):
        raise TransientError("员工开户未装配")
    result = await service.create(
        tenant_id=identity.tenant_id,
        actor_id=identity.employee.employee_id,
        user_id=identity.employee.user_id,
        username=command.username,
        name=command.name,
        password=command.password,
    )
    return result


@router.get("/employees", response_model=list[EmployeeView])
async def list_team_employees(
    identity: Annotated[RequestIdentity, _employee_list_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
) -> list[EmployeeView]:
    async with dependencies.employees(identity.tenant_id) as service:
        return await service.list_active(
            identity.tenant_id,
            actor=identity.employee_actor,
        )


@router.get("/territory", response_model=list[TerritoryAssignmentView])
async def list_team_territory(
    identity: Annotated[RequestIdentity, _territory_list_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
) -> list[TerritoryAssignmentView]:
    async with dependencies.employees(identity.tenant_id) as service:
        return await service.list_territory_matrix(
            identity.tenant_id,
            actor=identity.employee_actor,
        )


__all__ = ("router",)
