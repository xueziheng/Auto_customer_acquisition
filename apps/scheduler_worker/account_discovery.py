"""scheduler 的账户发现安全适配器；只做跨层装配，不承载业务规则。"""

from __future__ import annotations

from domains.demand.service import DemandService
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.service import EmployeeService
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, NeedHypothesisId, TenantId, UserId
from workflows.account_discovery.ports import (
    AccountDiscoveryActors,
    AccountDiscoveryTaskInput,
)


class DemandAccountDiscoveryTaskReader:
    """经 DemandService 构造模型安全输入，不接触需求仓储。"""

    def __init__(
        self,
        demand: DemandService,
        *,
        allowed_countries: tuple[str, ...],
    ) -> None:
        load = getattr(demand, "get_hypothesis_for_discovery", None)
        countries = tuple(dict.fromkeys(allowed_countries))
        if (
            not callable(load)
            or not countries
            or len(countries) > 100
            or any(
                not isinstance(country, str)
                or not country
                or country != country.strip()
                or len(country) > 64
                for country in countries
            )
        ):
            raise ValidationError("账户发现需求读取配置无效")
        self._demand = demand
        self._allowed_countries = countries

    async def load(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        acting_user: UserId,
    ) -> AccountDiscoveryTaskInput:
        del acting_user  # 身份已由 FindCompanyDetailsStep 在成本发生前重新校验。
        view = await self._demand.get_hypothesis_for_discovery(
            tenant_id, hypothesis_id
        )
        return AccountDiscoveryTaskInput(
            objective="依据已留痕的需求信号解析一个可消歧的目标企业",
            hypothesis={
                "hypothesis_id": view.hypothesis_id,
                "category": view.category,
                "reasoning": view.reasoning,
                "evidence": [
                    {
                        "signal_id": item.signal_id,
                        "summary": item.summary,
                        "source_url": item.source_url,
                    }
                    for item in view.evidence
                ],
                "source_signal_refs": list(view.source_signal_refs),
            },
            allowed_countries=self._allowed_countries,
        )


class BossAccountDiscoveryActorResolver:
    """按持久员工记录推导 Phase 1 boss actor；拒绝 header/context 自报角色。"""

    def __init__(self, employees: EmployeeService) -> None:
        if not callable(getattr(employees, "get_employee", None)):
            raise ValidationError("账户发现员工身份依赖无效")
        self._employees = employees
        self._lookup_actor = EmployeeActor(
            "system:scheduler-account-discovery",
            EmployeeScope.SYSTEM,
            "system",
        )

    async def resolve(
        self, tenant_id: TenantId, acting_user: UserId
    ) -> AccountDiscoveryActors:
        employee = await self._employees.get_employee(
            tenant_id,
            EmployeeId(str(acting_user)),
            actor=self._lookup_actor,
        )
        if (
            employee.tenant_id != tenant_id
            or str(employee.employee_id) != str(acting_user)
            or not employee.is_active
            or employee.role != "boss"
        ):
            raise PermissionDenied("账户发现仅允许活跃老板发起")
        actor_id = str(employee.employee_id)
        return AccountDiscoveryActors(
            employee=EmployeeActor(
                actor_id, EmployeeScope.TENANT, employee.role
            ),
            outreach=OutreachActor(
                actor_id,
                OutreachScope(level=OutreachScopeLevel.TENANT),
                employee.role,
            ),
        )


__all__ = (
    "BossAccountDiscoveryActorResolver",
    "DemandAccountDiscoveryTaskReader",
)
