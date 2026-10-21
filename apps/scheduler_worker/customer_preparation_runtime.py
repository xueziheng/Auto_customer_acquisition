"""准备组合借用canonical服务、原Gateway与单例激活，不自建调度循环。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from apps.composition_support.campaign_approval_reader import (
    CurrentCampaignApprovalReader,
)
from apps.composition_support.hunter_contacts import (
    HunterContactComposition,
    build_hunter_contact_tools,
)
from apps.composition_support.outreach_fact_readers import CurrentSendingIdentityReader
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.transport import BoundedObjectBlobTransport
from connectors.gmail.client import SecretResolver
from connectors.hunter.transport import HunterApiHttpTransport
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import Actor, EmployeeScope
from domains.outreach.service import OutreachService
from domains.products.service import EnterpriseKnowledgeServiceImpl
from domains.prospecting.inventory import CandidateAllocation
from domains.prospecting.inventory_followup import PreparationClaim
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.customer_assignment import CustomerAssignmentBridge
from infra.db.customer_preparation import PostgresAgentPreparationTransactions
from infra.db.customer_preparation_send import PreparationSendBindingReader
from infra.db.enterprise_knowledge import SqlAlchemyKnowledgeUnitOfWork
from infra.db.hunter_customer_quota import PostgresHunterCustomerQuota
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.hunter_customer_credentials import (
    HUNTER_CUSTOMER_KEY,
    HunterCustomerSecretResolver,
    PlatformHunterCustomerSecretResolver,
)
from infra.standalone.hunter_settings import HunterCustomerSettings
from infra.standalone.settings import StandaloneModelSettings, deployment_verification
from shared.errors import PermissionDenied
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderConfiguration,
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
    ProviderReadinessState,
)
from workflows.account_discovery.ports import ContactVerifier
from workflows.customer_preparation.authority import CustomerAgentScopeAuthority
from workflows.customer_preparation.send_guard import (
    CurrentCustomerPreparationSendGuard,
)
from workflows.customer_preparation.service import CustomerPreparationConsumer

from .account_discovery import (
    ComplianceCountryPolicyDecisionReader,
    DemandProspectingContactDiscoveryPolicy,
)
from .contact_hashing import DomainSeparatedContactValueHasher
from .customer_preparation_model import CustomerPreparationExtractor, preparation_run_id
from .inventory_web import InventoryWebTools, build_inventory_web

if TYPE_CHECKING:
    from .bootstrap import ResearchRuntimePorts
    from .main import InventoryDriverProtocol
    from .runtime import SchedulerCoreServices


@dataclass(frozen=True)
class PreparationRuntime:
    driver: CustomerPreparationConsumer
    activate: Callable[[], Awaitable[None]]


class InventoryWithCustomerPreparation:
    """复用同一singleton库存阶段，先消费一项持久准备再采集，无独立循环。"""
    def __init__(self, inventory: InventoryDriverProtocol | None, preparation: CustomerPreparationConsumer) -> None:
        self.inventory, self.preparation = inventory, preparation

    async def scan(self) -> int:
        # 已取得客户的持久任务先推进，新的搜索故障不能饿死旧准备项。
        await self.preparation.scan()
        return await self.inventory.scan() if self.inventory is not None else 0


def preparation_authority(core: SchedulerCoreServices, sessions: async_sessionmaker[AsyncSession],
                          tenant: TenantId, outreach: OutreachService, now: Callable[[], datetime]) -> CustomerAgentScopeAuthority:
    return CustomerAgentScopeAuthority(employees=core.employee_scope,
        lookup_actor=Actor("system:customer-preparation", EmployeeScope.SYSTEM, "system"), outreach=outreach,
        approvals=CurrentCampaignApprovalReader(tenant, core.approvals, now=now),
        knowledge=EnterpriseKnowledgeServiceImpl(lambda t: SqlAlchemyKnowledgeUnitOfWork(sessions, t)))


def build_preparation_send_guard(core: SchedulerCoreServices, sessions: async_sessionmaker[AsyncSession], tenant: TenantId,
                                 outreach: OutreachService, now: Callable[[], datetime]) -> CurrentCustomerPreparationSendGuard:
    return CurrentCustomerPreparationSendGuard(PreparationSendBindingReader(sessions, tenant),
        preparation_authority(core, sessions, tenant, outreach, now), core.employee_scope, core.prospecting, core.compliance, outreach)


async def build_customer_preparation(*, core: SchedulerCoreServices,
    sessions: async_sessionmaker[AsyncSession], control_sessions: async_sessionmaker[AsyncSession],
    control_tenant: TenantId, tenant_id: TenantId, outreach: OutreachService,
    existing_verifier: ContactVerifier | None, settings: StandaloneModelSettings,
    resolver: SecretResolver, fingerprints: HmacFingerprintProvider,
    research: ResearchRuntimePorts | None, owner: str,
    hunter: HunterCustomerSettings | None = None) -> PreparationRuntime:
    """只读取已有配置/验证事实；真实双工具装配完成后等待原singleton激活。"""
    now = lambda: datetime.now(UTC)
    authority = preparation_authority(core, sessions, tenant_id, outreach, now)
    backend = PostgresInventoryService(sessions, tenant_id, PostgresPublicCatalog(control_sessions, control_tenant),
        assignment_bridge=CustomerAssignmentBridge())
    repository = SqlModelConfigurationRepository(sessions, fingerprints, deployment_verification=deployment_verification(settings), deployment_provider=settings.provider)
    configuration = ModelConfigurationServiceImpl(repository, CurrentEmployeeIdentity(core.employee_scope,
        Actor("system:customer-preparation-model", EmployeeScope.SYSTEM, "system")), now=now)
    extractor = None
    bounded = research.bounded_object_transport if research is not None else None
    if bounded is None and research is not None and callable(getattr(research.object_transport, "get_bounded", None)):
        bounded = cast(BoundedObjectBlobTransport, research.object_transport)
    if research is not None and bounded is not None:
        artifacts = RawArtifactStoreImpl(lambda t: SqlAlchemyArtifactUnitOfWork(sessions, t), research.object_transport,  # type: ignore[arg-type]

            research.maximum_artifact_bytes, now, new_id, bounded_transport=bounded)
        async def web(claim: PreparationClaim, allocation: CandidateAllocation, guard: Callable[[], Awaitable[None]]) -> InventoryWebTools:
            return build_inventory_web(factory=sessions, tenant_id=tenant_id, run_id=preparation_run_id(claim),
                user_id=UserId(claim.authorization.actor_user_id), country=allocation.candidate.country,
                category=claim.authorization.query.product_tags[0], guard=guard, fingerprints=fingerprints,
                search_transport=research.search_transport, page_transport=research.page_transport,
                secret_resolver=research.secret_resolver, secret_ref=research.secret_ref, artifacts=artifacts, now=now,
                candidate_url=allocation.candidate.source_url)
        extractor = CustomerPreparationExtractor(web=web, artifacts=artifacts, configuration=configuration, settings=settings,
            resolver=resolver, sessions=sessions, fingerprints=fingerprints, lease_owner=owner, now=now)
    verifier = existing_verifier
    async def activate() -> None:
        return None
    if verifier is None and hunter is not None and hunter.enabled:
        if hunter.business_tenant_id != tenant_id:
            raise PermissionDenied("准备验证企业配置不一致")
        actor = ProviderReadinessActor("system:scheduler-customer-verification", tenant_id,
            frozenset({ProviderReadinessPermission.READ, ProviderReadinessPermission.COMPOSE}))
        readiness = ProviderReadinessServiceImpl(lambda t: SqlAlchemyProviderReadinessUnitOfWork(sessions, t, now=now), runtime_actor=actor, now=now)
        expected = ProviderConfiguration.hunter_contacts(hunter.configuration_version, hunter.api_key_version)
        snapshot = await readiness.get_snapshot(tenant_id, HUNTER_CONTACT_CAPABILITIES, actor=actor)
        if snapshot.configuration == expected and snapshot.state in {ProviderReadinessState.READY, ProviderReadinessState.RUNTIME_NOT_COMPOSED}:
            from domains.compliance.permissions import ComplianceActor, ComplianceScope
            secret: PlatformHunterCustomerSecretResolver | HunterCustomerSecretResolver
            if hunter.credential_source == "platform":
                secret = PlatformHunterCustomerSecretResolver(hunter)
            else:
                if hunter.secret_file is None:
                    raise PermissionDenied("验证凭证来源未配置")
                secret = HunterCustomerSecretResolver(hunter.secret_file)
            tools = build_hunter_contact_tools(factory=sessions, tenant_id=tenant_id, tool_user=UserId(new_id("usr")), fingerprints=fingerprints,
                outreach=outreach, prospecting=core.prospecting,
                country_policy=ComplianceCountryPolicyDecisionReader(core.compliance, ComplianceActor("system:customer-verification", tenant_id, ComplianceScope.SYSTEM, "system")),
                composition=HunterContactComposition(DemandProspectingContactDiscoveryPolicy(core.demand, core.prospecting),
                    PostgresHunterCustomerQuota(sessions, tenant_id, maximum_daily_verifications=hunter.maximum_daily_verifications, now=now)),
                secret_resolver=secret, secret_ref=HUNTER_CUSTOMER_KEY, transport=HunterApiHttpTransport(), snapshot=snapshot,
                readiness_guard=readiness, lease_duration=timedelta(seconds=60), now=now)
            if tools.manifest_ids != ("contact.enrich", "contact.verify") or tools.registered_configuration_hash != expected.configuration_hash:
                raise PermissionDenied("准备验证实际组合不完整")
            verifier = tools.verifier
            async def activate() -> None:
                await readiness.mark_runtime_composed(tenant_id, expected.configuration_hash, actor=actor,
                    idempotency_key=IdempotencyKey("scheduler-customer-verification:" + expected.configuration_hash))
    # 与canonical Prospecting共用同一个运行时hasher；由组合显式提供，不读取服务私有属性。
    return PreparationRuntime(CustomerPreparationConsumer(tenant_id=tenant_id, queue=backend, authority=authority,
        employees=core.employee_scope, prospecting=core.prospecting, outreach=outreach, compliance=core.compliance,
        senders=CurrentSendingIdentityReader(tenant_id, core.sending, now=now),
        transactions=PostgresAgentPreparationTransactions(sessions, DomainSeparatedContactValueHasher(fingerprints), now=now), extractor=extractor, verifier=verifier, now=now), activate)
