"""Team & Territory：活跃员工与确定性分配矩阵查询。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from domains.employees.permissions import EmployeeAction
from domains.employees.schemas import EmployeeView, TerritoryAssignmentView

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
