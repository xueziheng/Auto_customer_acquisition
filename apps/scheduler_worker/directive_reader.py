"""把指令域公开 DTO 适配为需求探索 workflow 的确认计划。"""

from __future__ import annotations

from typing import Protocol

from apps.composition_support.employee_readers import CurrentEmployeeUserReader
from domains.directives.service import DirectiveService
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.schemas import EmployeeView
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from workflows.demand_discovery.ports import (
    DemandDiscoveryPlan,
    DiscoverySearchQuery,
)
from workflows.sourcing_case.application import (
    DirectiveSourcingAdmissionPolicyReader,
    SourcingAdmissionPolicyRead,
)


class DirectiveEmployeeLookup(Protocol):
    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView: ...


class SchedulerDirectiveEmployeeReader:
    """用 scheduler 的 SYSTEM 员工读取能力满足指令域最窄依赖。"""

    def __init__(
        self,
        employees: DirectiveEmployeeLookup,
        actor: EmployeeActor,
        tenant_id: TenantId,
    ) -> None:
        if (
            not callable(getattr(employees, "get_employee", None))
            or not isinstance(actor, EmployeeActor)
            or actor.scope.value != "system"
            or actor.role != "system"
            or not isinstance(tenant_id, str)
            or not tenant_id
        ):
            raise ValidationError("scheduler 指令员工读取依赖无效")
        self._employees = employees
        self._actor = actor
        self._tenant_id = tenant_id

    async def is_active_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> bool:
        employee = await self._read(tenant_id, employee_id)
        return employee is not None and employee.is_active and employee.role == "boss"

    async def names_for(
        self, tenant_id: TenantId, employee_ids: tuple[EmployeeId, ...]
    ) -> dict[EmployeeId, str]:
        result: dict[EmployeeId, str] = {}
        for employee_id in dict.fromkeys(employee_ids):
            employee = await self._read(tenant_id, employee_id)
            if employee is not None and employee.is_active:
                result[employee.employee_id] = employee.name
        return result

    async def _read(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> EmployeeView | None:
        if tenant_id != self._tenant_id:
            return None
        employee = await self._employees.get_employee(
            tenant_id, employee_id, actor=self._actor
        )
        if (
            not isinstance(employee, EmployeeView)
            or employee.tenant_id != tenant_id
            or employee.employee_id != employee_id
        ):
            return None
        return employee


class DirectiveDemandDiscoveryTaskReader:
    def __init__(
        self,
        directives: DirectiveService,
        users: CurrentEmployeeUserReader | None = None,
    ) -> None:
        if not isinstance(directives, DirectiveService):
            raise ValidationError("需求探索指令读取器依赖无效")
        self._directives = directives
        self._users = users

    async def load_confirmed(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        acting_user: UserId,
    ) -> DemandDiscoveryPlan:
        if self._users is None:
            raise ValidationError("需求探索员工映射依赖未配置")
        employee = await self._users.get_for_user(tenant_id, acting_user)
        if employee.role != "boss":
            raise ValidationError("需求探索员工身份不可用")
        plan = await self._directives.get_confirmed_discovery_plan(
            tenant_id,
            proposal_id,
            employee.employee_id,
        )
        return DemandDiscoveryPlan(
            objective=plan.objective,
            queries=tuple(
                DiscoverySearchQuery(
                    query=item.query,
                    country=item.country,
                    category=item.category,
                    limit=item.limit,
                    discovery_lane=item.discovery_lane,
                )
                for item in plan.queries
            ),
            target_countries=plan.target_countries,
            target_categories=plan.target_categories,
            excluded_countries=plan.excluded_countries,
            excluded_categories=plan.excluded_categories,
            max_search_queries=plan.max_search_queries,
            max_pages_read=plan.max_pages_read,
            max_signals=plan.max_signals,
            max_hypotheses=plan.max_hypotheses,
            minimum_confidence_tier=plan.minimum_confidence_tier,
            strategy_group=plan.strategy_group,
            campaign_id=plan.campaign_id,
            role_hints=plan.role_hints,
            assessment_ref=plan.assessment_ref,
            execution_mode=plan.execution_mode,
        )


__all__ = (
    "DirectiveDemandDiscoveryTaskReader",
    "DirectiveSourcingAdmissionPolicyReader",
    "SchedulerDirectiveEmployeeReader",
    "SourcingAdmissionPolicyRead",
)
