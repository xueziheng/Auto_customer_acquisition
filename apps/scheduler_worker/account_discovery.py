"""scheduler 的账户发现安全适配器；只做跨层装配，不承载业务规则。"""

from __future__ import annotations

from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.compliance.schemas import CountryPolicyAction, CountryPolicyDecision
from domains.compliance.service import ComplianceService
from domains.demand.service import DemandService
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.service import EmployeeService
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.prospecting.service import ProspectingService
from shared.errors import (
    PermissionDenied,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import (
    EmployeeId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    UserId,
)
from tool_gateway.checks.contact_provider import ContactDiscoveryPreflight
from workflows.account_discovery.ports import (
    AccountDiscoveryActors,
    AccountDiscoveryOrganizationFact,
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
            hypothesis_id=NeedHypothesisId(view.hypothesis_id),
            organization=AccountDiscoveryOrganizationFact(
                account_id=ProspectAccountId(view.account_id),
                entity_name=view.organization_name,
                country=view.country,
                website_domain=view.website_domain,
            ),
            category=view.category,
            source_signal_refs=view.source_signal_refs,
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


class DemandProspectingContactDiscoveryPolicy:
    """把需求假设与已消歧企业绑定成 contact.enrich 的可信 preflight。"""

    def __init__(self, demand: DemandService, prospecting: ProspectingService) -> None:
        if not callable(
            getattr(demand, "get_hypothesis_for_discovery", None)
        ) or not callable(getattr(prospecting, "get_account_detail", None)):
            raise ValidationError("联系人发现 Playbook 依赖无效")
        self._demand = demand
        self._prospecting = prospecting

    async def preflight(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
    ) -> ContactDiscoveryPreflight:
        hypothesis = await self._demand.get_hypothesis_for_discovery(
            tenant_id, hypothesis_id
        )
        if hypothesis.account_id != str(account_id):
            raise ValidationError("需求假设与目标企业不匹配")
        detail = await self._prospecting.get_account_detail(tenant_id, account_id)
        account = detail.account
        if account.tenant_id != tenant_id or account.account_id != account_id:
            raise TenantIsolationViolation("联系人发现企业租户绑定无效")
        if account.website_domain is None:
            raise ValidationError("联系人发现企业缺少官网域名")
        return ContactDiscoveryPreflight(
            tenant_id=tenant_id,
            hypothesis_id=hypothesis_id,
            account_id=account_id,
            category=hypothesis.category,
            country=account.country,
            website_domain=account.website_domain,
        )


class ComplianceCountryPolicyDecisionReader:
    """以显式 SYSTEM 身份调用合规域公共决策接口。"""

    def __init__(
        self,
        compliance: ComplianceService,
        actor: ComplianceActor,
    ) -> None:
        if (
            not callable(getattr(compliance, "get_country_policy_decision", None))
            or not isinstance(actor, ComplianceActor)
            or actor.scope is not ComplianceScope.SYSTEM
            or actor.role != "system"
        ):
            raise ValidationError("scheduler 国家政策决策依赖无效")
        self._compliance = compliance
        self._actor = actor

    async def decision(
        self,
        tenant_id: TenantId,
        country: str,
        action: CountryPolicyAction,
    ) -> CountryPolicyDecision:
        if tenant_id != self._actor.tenant_id:
            raise TenantIsolationViolation("scheduler 国家政策租户不匹配")
        return await self._compliance.get_country_policy_decision(
            tenant_id,
            country,
            action,
            actor=self._actor,
        )


__all__ = (
    "BossAccountDiscoveryActorResolver",
    "ComplianceCountryPolicyDecisionReader",
    "DemandAccountDiscoveryTaskReader",
    "DemandProspectingContactDiscoveryPolicy",
)
