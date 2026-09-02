"""把指令域公开 DTO 适配为需求探索 workflow 的确认计划。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domains.directives.schemas import DirectiveView
from domains.directives.service import DirectiveService
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.schemas import EmployeeView
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from workflows.demand_discovery.ports import (
    DemandDiscoveryPlan,
    DiscoverySearchQuery,
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
    def __init__(self, directives: DirectiveService) -> None:
        if not isinstance(directives, DirectiveService):
            raise ValidationError("需求探索指令读取器依赖无效")
        self._directives = directives

    async def load_confirmed(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        acting_user: UserId,
    ) -> DemandDiscoveryPlan:
        plan = await self._directives.get_confirmed_discovery_plan(
            tenant_id,
            proposal_id,
            EmployeeId(str(acting_user)),
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


@dataclass(frozen=True)
class SourcingAdmissionPolicyRead:
    """当前 active Directive 中经二次校验的寻源准入段。"""

    directive_id: str
    directive_version: int
    enabled: bool
    batch_limit: int


class DirectiveSourcingAdmissionPolicyReader:
    """把 active、老板确认过的 Directive 投影为自动准入策略。"""

    def __init__(self, directives: DirectiveService) -> None:
        if not isinstance(directives, DirectiveService):
            raise ValidationError("寻源准入指令读取器依赖无效")
        self._directives = directives

    async def read(
        self, tenant_id: TenantId
    ) -> SourcingAdmissionPolicyRead | None:
        """仅明确无 active/无完整段返回 None；其他不确定性统一暂态失败。"""

        read_error: Exception | None = None
        directive: DirectiveView | None = None
        try:
            directive = await self._directives.get_active(tenant_id)
        except Exception as error:  # noqa: BLE001 - 跨域读取只向外暴露固定暂态状态
            read_error = error
        if read_error is not None:
            raise TransientError("寻源准入策略状态暂不可确认") from None
        if directive is None:
            return None
        if not isinstance(directive, DirectiveView):
            raise TransientError("寻源准入策略状态暂不可确认") from None
        section = (
            directive.sourcing_admission_mode,
            directive.automatic_sourcing_admission_enabled,
            directive.sourcing_admission_batch_limit,
        )
        if section == (None, None, None):
            return None
        if (
            not isinstance(directive.directive_id, str)
            or not directive.directive_id
            or directive.directive_id != directive.directive_id.strip()
            or len(directive.directive_id) > 200
            or type(directive.version) is not int
            or directive.version < 1
            or directive.superseded_at is not None
            or directive.sourcing_admission_mode != "cluster_ranked"
            or type(directive.automatic_sourcing_admission_enabled) is not bool
            or type(directive.sourcing_admission_batch_limit) is not int
            or not 1 <= directive.sourcing_admission_batch_limit <= 50
        ):
            raise TransientError("寻源准入策略状态暂不可确认") from None
        return SourcingAdmissionPolicyRead(
            directive_id=directive.directive_id,
            directive_version=directive.version,
            enabled=directive.automatic_sourcing_admission_enabled,
            batch_limit=directive.sourcing_admission_batch_limit,
        )


__all__ = (
    "DirectiveDemandDiscoveryTaskReader",
    "DirectiveSourcingAdmissionPolicyReader",
    "SchedulerDirectiveEmployeeReader",
    "SourcingAdmissionPolicyRead",
)
