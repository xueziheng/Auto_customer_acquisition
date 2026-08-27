"""验收入口的真实只读依赖；不创建政策、老板、提案或默认许可。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from domains.directives.service import DirectiveService
from domains.directives.service_impl import DirectiveServiceImpl
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
    Phase1OrganizationAuthorizer,
)
from domains.organization.service import OrganizationService
from domains.organization.service_impl import OrganizationServiceImpl
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.directive_uow import SqlAlchemyDirectiveUnitOfWork
from infra.db.organization_uow import SqlAlchemyOrganizationUnitOfWork
from infra.db.tables import EmployeeRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from workflows.demand_discovery.ports import DemandDiscoveryPlan

from .account_discovery import ComplianceCountryPolicyDecisionReader
from .directive_reader import DirectiveDemandDiscoveryTaskReader


class AcceptanceEmployees:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def is_active_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> bool:
        async with self._factory() as session:
            return (
                await session.execute(
                    select(EmployeeRow.employee_id).where(
                        EmployeeRow.tenant_id == tenant_id,
                        EmployeeRow.employee_id == employee_id,
                        EmployeeRow.is_active.is_(True),
                        EmployeeRow.role == "boss",
                    )
                )
            ).first() is not None

    async def names_for(
        self, tenant_id: TenantId, employee_ids: tuple[EmployeeId, ...]
    ) -> dict[EmployeeId, str]:
        async with self._factory() as session:
            return {
                EmployeeId(row.employee_id): row.name
                for row in (
                    await session.execute(
                        select(EmployeeRow.employee_id, EmployeeRow.name).where(
                            EmployeeRow.tenant_id == tenant_id,
                            EmployeeRow.employee_id.in_(employee_ids),
                        )
                    )
                ).all()
            }


class AcceptanceTaskReader:
    def __init__(
        self,
        tenant_id: TenantId,
        directives: DirectiveService,
        employees: AcceptanceEmployees,
    ) -> None:
        self._tenant, self.directives, self._employees = (
            tenant_id,
            directives,
            employees,
        )

    async def load_confirmed(
        self, tenant_id: TenantId, proposal_id: str, acting_user: UserId
    ) -> DemandDiscoveryPlan:
        """确认人仍在职且是老板，提案仍为当前active；失效提案不能继续花费。"""
        if tenant_id != self._tenant or not await self._employees.is_active_boss(
            tenant_id, EmployeeId(acting_user)
        ):
            raise PermissionDenied("来源验收仅限当前租户在职老板")
        active = await self.directives.get_active(tenant_id)
        if active is None or active.source_proposal_id != proposal_id:
            raise ValidationError("来源验收提案不是当前生效指令")
        plan = await DirectiveDemandDiscoveryTaskReader(self.directives).load_confirmed(
            tenant_id, proposal_id, acting_user
        )
        if plan.execution_mode != "research_only":
            raise ValidationError("来源验收必须为只研究提案")
        return plan


class AcceptancePlaybookReader:
    def __init__(
        self,
        tenant: TenantId,
        organization: OrganizationService,
        employees: AcceptanceEmployees,
    ) -> None:
        self._tenant, self._organization, self._employees = (
            tenant,
            organization,
            employees,
        )
        self._actor: EmployeeId | None = None

    def bind_actor(self, actor: EmployeeId) -> None:
        """仅由已验证老板的受信CLI组合绑定，不接收HTTP/模型上下文。"""
        self._actor = actor

    async def allows_research(
        self, tenant_id: TenantId, category: str, country: str
    ) -> bool:
        if (
            tenant_id != self._tenant
            or self._actor is None
            or not await self._employees.is_active_boss(tenant_id, self._actor)
        ):
            return False
        book = await self._organization.get_playbook(
            tenant_id,
            actor=OrganizationActor(
                str(self._actor),
                OrganizationScope(OrganizationScopeLevel.TENANT, tenant_id),
                "boss",
            ),
        )
        return book.is_category_allowed(category) and book.is_country_allowed(country)


def build_acceptance_readers(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> tuple[
    AcceptanceTaskReader,
    AcceptancePlaybookReader,
    ComplianceCountryPolicyDecisionReader,
]:
    """只装配已有域服务与tenant-bound UoW，真实缺配置由域服务拒绝。"""
    employees = AcceptanceEmployees(factory)
    directives = DirectiveServiceImpl(
        lambda bound: SqlAlchemyDirectiveUnitOfWork(factory, bound),  # type: ignore[arg-type, return-value]
        employees,
    )
    organization = OrganizationServiceImpl(
        lambda bound: SqlAlchemyOrganizationUnitOfWork(factory, bound),  # type: ignore[arg-type, return-value]
        Phase1OrganizationAuthorizer(tenant),
    )
    compliance = ComplianceServiceImpl(
        lambda bound: SqlAlchemyComplianceUnitOfWork(factory, bound),
        Phase1ComplianceAuthorizer(tenant),
    )
    return (
        AcceptanceTaskReader(tenant, directives, employees),
        AcceptancePlaybookReader(tenant, organization, employees),
        ComplianceCountryPolicyDecisionReader(
            compliance,
            ComplianceActor(
                "system:research-source-acceptance",
                tenant,
                ComplianceScope.SYSTEM,
                "system",
            ),
        ),
    )
