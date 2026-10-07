"""当前进程的规范服务装配；端口仅由宿主显式提供，不读取环境或新建连接池。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.account_discovery.agent import AccountDiscoveryAgent
from agent_runtime.account_discovery.model_port import (
    StructuredAccountDiscoveryModelPort,
)
from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
from agent_runtime.demand_intelligence.model_port import (
    StructuredDemandIntelligenceModelPort,
)
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.model_client import StructuredJsonModelClient
from apps.composition_support.campaign_approval_reader import (
    CurrentCampaignApprovalReader,
)
from apps.composition_support.delivery_material_reader import (
    CurrentDeliveryMaterialReader,
)
from apps.composition_support.employee_readers import (
    CurrentEmployeeUserReader,
    EmployeeServiceScope,
    RequestScopedHandoffEmployeeReader,
)
from apps.composition_support.outreach_fact_readers import (
    CurrentContactEligibilityReader,
    CurrentReplyStatusReader,
    CurrentSendingIdentityReader,
)
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.transport import ObjectBlobTransport
from connectors.gmail.client import SecretResolver
from connectors.gmail.transport import GmailHttpTransport
from connectors.tavily.transport import TavilySearchTransport
from connectors.web_search.transport import PublicPageTransport
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView, OwnershipLockView
from domains.employees.service import EmployeeService
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import (
    OpportunityScope,
    Phase1OpportunityAuthorizer,
    ScopeLevel,
)
from domains.opportunities.permissions import StandardAuditLogger as OpportunityAudit
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service import OpportunityService
from domains.opportunities.service_impl import HandoffPolicy, OpportunityServiceImpl
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
)
from domains.organization.service import OrganizationService
from domains.outreach.service import OutreachService
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from shared.errors import ValidationError
from shared.events.catalog import DomainEvent, HandoffRequested
from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TenantId,
    UserId,
    new_id,
)
from workflows.account_discovery.ports import ContactEnricher, ContactVerifier
from workflows.assistant.ports import AssistantRuntimePorts
from workflows.demand_discovery.ports import DemandIntelligenceCapability

from .account_discovery import (
    BossAccountDiscoveryActorResolver,
    DemandAccountDiscoveryTaskReader,
)
from .config import SchedulerWorkerConfig
from .contact_binding import ContactRuntimeResources
from .directive_reader import DirectiveDemandDiscoveryTaskReader
from .notification_projection import NotificationAudienceMember
from .reply_binding import ReplyRuntimeResources
from .runtime import (
    AccountDiscoveryComposition,
    CampaignMessagingComposition,
    DemandDiscoveryComposition,
    ReplyQualificationComposition,
    SchedulerCoreServices,
    SchedulerDomainDependencies,
)
from .sourcing_runtime import (
    BoundSourcingExtractionModelPort,
    SourcingResearchComposition,
)
from .web_discovery import WebDiscoveryToolComposition


class CurrentNotificationAudience:
    """通知当前 active 管理者；接管事件补充当前账户归属及其经理。"""

    def __init__(
        self,
        tenant_id: TenantId,
        employees: EmployeeServiceScope,
        opportunities: OpportunityService,
    ) -> None:
        self._tenant, self._employees, self._opportunities = (
            tenant_id,
            employees,
            opportunities,
        )
        self._actor = EmployeeActor(
            "system:notification-audience", EmployeeScope.SYSTEM, "system"
        )

    async def recipients_for(
        self, tenant_id: TenantId, event: DomainEvent
    ) -> tuple[NotificationAudienceMember, ...]:
        if tenant_id != self._tenant or event.tenant_id != tenant_id:
            raise ValidationError("当前通知受众不可用")
        account = None
        if isinstance(event, HandoffRequested) and event.opportunity_id is not None:
            opportunity = await self._opportunities.get_notification_audience_target(
                tenant_id,
                event.opportunity_id,
                actor=OpportunityActor(
                    "system:notification-audience",
                    OpportunityScope(
                        level=ScopeLevel.SYSTEM,
                        notification_opportunity_id=event.opportunity_id,
                    ),
                    "system",
                ),
            )
            account = ProspectAccountId(opportunity.account_id)
        async with self._employees(tenant_id) as service:
            employees = await service.list_active(tenant_id, actor=self._actor)
            ownership = (
                None
                if account is None
                else await service.get_notification_owner(
                    tenant_id,
                    account,
                    actor=EmployeeActor(
                        "system:notification-audience",
                        EmployeeScope.SYSTEM,
                        "system",
                        notification_account_id=account,
                    ),
                )
            )
        ids = {
            e.employee_id
            for e in employees
            if e.tenant_id == tenant_id
            and e.is_active
            and e.role in {"boss", "manager"}
        }
        if ownership is not None:
            ids.add(ownership)
        active = {
            e.employee_id for e in employees if e.tenant_id == tenant_id and e.is_active
        }
        return tuple(
            NotificationAudienceMember(tenant_id, EmployeeId(e))
            for e in sorted(ids & active)
        )


@dataclass(frozen=True)
class ResearchRuntimePorts:
    """只含受信宿主显式提供的外部端口与研究边界；对象客户端由宿主拥有。"""

    model_client: StructuredJsonModelClient
    model: str
    user_id: UserId
    search_transport: TavilySearchTransport
    page_transport: PublicPageTransport
    object_transport: ObjectBlobTransport
    maximum_artifact_bytes: int
    secret_ref: str
    secret_resolver: SecretResolver
    exclusive_account_confirmed: bool
    capability: DemandIntelligenceCapability | None = None


@dataclass(frozen=True)
class SourcingRuntimePorts:
    """现有寻源链的外部端口，准入配置仍由 scheduler config 显式控制。"""

    user_id: UserId
    search_transport: TavilySearchTransport
    page_transport: PublicPageTransport
    object_transport: ObjectBlobTransport
    maximum_artifact_bytes: int
    model_port: BoundSourcingExtractionModelPort


@dataclass(frozen=True)
class ContactRuntimePorts:
    """联系人 Provider 端口；业务服务和身份由本次 bootstrap 构造。"""

    model_client: StructuredJsonModelClient
    model: str
    allowed_countries: tuple[str, ...]
    enricher: ContactEnricher
    verifier: ContactVerifier


class CurrentResearchPlaybookReader:
    def __init__(
        self,
        tenant: TenantId,
        organization: OrganizationService,
        users: CurrentEmployeeUserReader,
        user: UserId,
    ) -> None:
        self._tenant, self._organization, self._users, self._user = (
            tenant,
            organization,
            users,
            user,
        )

    async def allows_research(
        self, tenant_id: TenantId, category: str, country: str
    ) -> bool:
        if tenant_id != self._tenant:
            return False
        employee = await self._users.get_for_user(tenant_id, self._user)
        if employee.role != "boss":
            return False
        book = await self._organization.get_playbook(
            tenant_id,
            actor=OrganizationActor(
                str(employee.employee_id),
                OrganizationScope(OrganizationScopeLevel.TENANT, tenant_id),
                "boss",
            ),
        )
        return book.is_category_allowed(category) and book.is_country_allowed(country)


@dataclass(frozen=True)
class CanonicalSchedulerBootstrap:
    """完整基础服务与显式可选发送组。输入外部端口由调用者拥有。"""

    scoring_policy: ScoringPolicy
    handoff_policy: HandoffPolicy
    sourcing: SourcingRuntimePorts | None = None
    assistant_factory: Callable[[SchedulerCoreServices, async_sessionmaker[AsyncSession], OpportunityService], AssistantRuntimePorts] | None = None
    research_enabled: bool = False
    research: ResearchRuntimePorts | None = None
    research_factory: Callable[[SchedulerCoreServices, async_sessionmaker[AsyncSession]], ResearchRuntimePorts] | None = None
    contacts_enabled: bool = False
    contacts: ContactRuntimePorts | None = None
    contacts_factory: (
        Callable[
            [SchedulerCoreServices, OutreachService, ContactRuntimeResources],
            ContactRuntimePorts,
        ]
        | None
    ) = None
    campaign_enabled: bool = False
    gmail_transport: GmailHttpTransport | None = None
    secret_resolver: SecretResolver | None = None
    reply_factory: (
        Callable[
            [SchedulerCoreServices, OutreachService, ReplyRuntimeResources],
            ReplyQualificationComposition,
        ]
        | None
    ) = None

    def __post_init__(self) -> None:
        if self.research_factory is not None and (not self.research_enabled or self.research is not None):
            raise ValidationError("研究工厂必须显式启用且不能同时指定旧端口")
        if self.research_enabled and self.research_factory is None and not isinstance(
            self.research, ResearchRuntimePorts
        ):
            raise ValidationError("scheduler 研究依赖未完整配置")
        if self.contacts_factory is not None and (
            not callable(self.contacts_factory)
            or self.contacts is not None
            or not self.contacts_enabled
            or not self.campaign_enabled
        ):
            raise ValidationError("scheduler 联系人工厂配置无效")
        if self.contacts_enabled and (
            (
                not isinstance(self.contacts, ContactRuntimePorts)
                and self.contacts_factory is None
            )
            or not self.campaign_enabled
        ):
            raise ValidationError("scheduler 联系人依赖未完整配置")
        if any(
            type(value) is not bool
            for value in (
                self.campaign_enabled,
                self.research_enabled,
                self.contacts_enabled,
            )
        ):
            raise ValidationError("scheduler Campaign 配置无效")
        if self.campaign_enabled and (
            not isinstance(self.gmail_transport, GmailHttpTransport)
            or not isinstance(self.secret_resolver, SecretResolver)
        ):
            raise ValidationError("scheduler Campaign 发送依赖未完整配置")
        if self.reply_factory is not None and not self.campaign_enabled:
            raise ValidationError("scheduler 回复依赖未完整配置")

    def build_assistant(self, core: SchedulerCoreServices, sessions: async_sessionmaker[AsyncSession], opportunities: OpportunityService) -> AssistantRuntimePorts | None:
        """只构造独立端口；调度器稍后绑定同一个 engine。"""
        return self.assistant_factory(core, sessions, opportunities) if self.assistant_factory is not None else None

    def build_base(
        self,
        config: SchedulerWorkerConfig,
        sessions: async_sessionmaker[AsyncSession],
        core: SchedulerCoreServices,
        *,
        now: Callable[[], datetime],
    ) -> SchedulerDomainDependencies:
        tenant = config.tenant_id
        employees = core.employee_scope
        opportunities = cast(
            OpportunityService,
            OpportunityServiceImpl(
                lambda: SqlAlchemyOpportunityUnitOfWork(sessions, tenant),  # type: ignore[arg-type, return-value]
                OpportunityScorerImpl(self.scoring_policy),
                self.handoff_policy,
                authorizer=Phase1OpportunityAuthorizer(tenant),
                audit=OpportunityAudit(),
                now=now,
            ),
        )
        campaign = None
        if self.campaign_enabled:
            if self.secret_resolver is None or self.gmail_transport is None:
                raise ValidationError("scheduler Campaign 发送依赖未完整配置")
            campaign = CampaignMessagingComposition(
                CurrentContactEligibilityReader(
                    tenant, core.prospecting, core.demand, now=now
                ),
                CurrentSendingIdentityReader(tenant, core.sending, now=now),
                CurrentCampaignApprovalReader(tenant, core.approvals, now=now),
                CurrentReplyStatusReader(
                    tenant, core.prospecting, core.conversations, now=now
                ),
                CurrentDeliveryMaterialReader(tenant, core.prospecting, core.sending),
                self.secret_resolver,
                self.gmail_transport,
            )
        account = None
        if self.contacts_enabled and self.contacts_factory is None:
            contacts = self.contacts
            if contacts is None:
                raise ValidationError("scheduler 联系人依赖未完整配置")
            account = self._account_composition(core, contacts)
        discovery = None
        if self.research_enabled:
            research = self.research_factory(core, sessions) if self.research_factory is not None else self.research
            if research is None:
                raise ValidationError("scheduler 研究依赖未完整配置")
            actor = EmployeeActor(
                "system:research-bootstrap", EmployeeScope.SYSTEM, "system"
            )
            users = CurrentEmployeeUserReader(employees, actor)
            artifacts = RawArtifactStoreImpl(
                lambda requested: SqlAlchemyArtifactUnitOfWork(sessions, requested),  # type: ignore[arg-type]
                research.object_transport,
                research.maximum_artifact_bytes,
                now,
                new_id,
            )
            web = WebDiscoveryToolComposition(
                CurrentResearchPlaybookReader(
                    tenant, core.organization, users, research.user_id
                ),
                research.secret_resolver,
                research.secret_ref,
                research.search_transport,
                research.page_transport,
                artifacts,
                provider="tavily",
                exclusive_account_confirmed=research.exclusive_account_confirmed,
            )
            discovery = DemandDiscoveryComposition(
                DirectiveDemandDiscoveryTaskReader(core.directives, users),
                research.capability or DemandIntelligenceAgent(
                    research.model,
                    StructuredDemandIntelligenceModelPort(
                        research.model_client, research.model
                    ),
                    None,
                    CredentialMarkerGuard(),
                ),
                core.demand,
                core.prospecting,
                web,
            )
        sourcing = None
        if config.sourcing is not None:
            ports = self.sourcing
            if ports is None:
                raise ValidationError("scheduler 寻源依赖未完整配置")
            users = CurrentEmployeeUserReader(
                employees,
                EmployeeActor(
                    "system:sourcing-bootstrap", EmployeeScope.SYSTEM, "system"
                ),
            )
            artifacts = RawArtifactStoreImpl(
                lambda requested: SqlAlchemyArtifactUnitOfWork(sessions, requested),  # type: ignore[arg-type]
                ports.object_transport,
                ports.maximum_artifact_bytes,
                now,
                new_id,
            )
            sourcing = SourcingResearchComposition(
                CurrentResearchPlaybookReader(
                    tenant, core.organization, users, ports.user_id
                ),
                ports.search_transport,
                ports.page_transport,
                artifacts,
                ports.model_port,
            )
        return SchedulerDomainDependencies(
            opportunities,
            RequestScopedHandoffEmployeeReader(employees),
            CurrentNotificationAudience(tenant, employees, opportunities),
            campaign_messaging=campaign,
            account_discovery=account,
            demand_discovery=discovery,
            sourcing_case=sourcing,
        )

    def build_contacts(
        self,
        core: SchedulerCoreServices,
        outreach: OutreachService | None,
        *,
        resources: ContactRuntimeResources,
    ) -> AccountDiscoveryComposition | None:
        """默认不覆盖静态组合；late factory必须绑定本runtime的发送服务。"""
        if self.contacts_factory is None:
            return None
        if outreach is None:
            raise ValidationError("scheduler 联系人工厂未绑定发送服务")
        ports = self.contacts_factory(core, outreach, resources)
        if not isinstance(ports, ContactRuntimePorts):
            raise ValidationError("scheduler 联系人工厂返回无效")
        return self._account_composition(core, ports)

    @staticmethod
    def _account_composition(
        core: SchedulerCoreServices, contacts: ContactRuntimePorts
    ) -> AccountDiscoveryComposition:
        scoped_employees = cast(
            EmployeeService, ScopedDiscoveryEmployees(core.employee_scope)
        )
        return AccountDiscoveryComposition(
            DemandAccountDiscoveryTaskReader(
                core.demand, allowed_countries=contacts.allowed_countries
            ),
            AccountDiscoveryAgent(
                contacts.model,
                StructuredAccountDiscoveryModelPort(
                    contacts.model_client, contacts.model
                ),
                None,
                CredentialMarkerGuard(),
            ),
            core.prospecting,
            scoped_employees,
            BossAccountDiscoveryActorResolver(scoped_employees),
            enricher=contacts.enricher,
            verifier=contacts.verifier,
        )

    def build_reply(
        self,
        core: SchedulerCoreServices,
        outreach: OutreachService | None,
        *,
        resources: ReplyRuntimeResources | None = None,
    ) -> ReplyQualificationComposition | None:
        if self.reply_factory is None:
            return None
        if outreach is None or resources is None:
            raise ValidationError("scheduler 回复依赖未完整配置")
        return self.reply_factory(core, outreach, resources)


class ScopedDiscoveryEmployees(RequestScopedHandoffEmployeeReader):
    """账户发现按调用托管员工服务，归属判断仍完全委托领域。"""

    async def list_active(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[EmployeeView]:
        async with self._scope(tenant_id) as service:
            return await service.list_active(tenant_id, actor=actor)

    async def resolve_owner(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        *,
        actor: EmployeeActor,
        country: str,
        need_category: str | None = None,
        buyer_type: str | None = None,
        language: str | None = None,
        timezone: str | None = None,
        boss_override: EmployeeId | None = None,
    ) -> OwnershipLockView:
        async with self._scope(tenant_id) as service:
            return await service.resolve_owner(
                tenant_id,
                account_id,
                actor=actor,
                country=country,
                need_category=need_category,
                buyer_type=buyer_type,
                language=language,
                timezone=timezone,
                boss_override=boss_override,
            )
