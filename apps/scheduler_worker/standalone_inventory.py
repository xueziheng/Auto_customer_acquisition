"""独立后台库存机械装配；显式注入控制库、公司库和研究端口。"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.deepseek_search.client import DeepSeekSearchConnector
from connectors.deepseek_search.transport import DeepSeekSearchApiTransport
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import Actor, EmployeeScope
from domains.products.service import EnterpriseKnowledgeServiceImpl
from domains.prospecting.service import AcquisitionJob
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.customer_assignment import CustomerAssignmentBridge
from infra.db.enterprise_knowledge import SqlAlchemyKnowledgeUnitOfWork
from infra.db.inventory_search_budget import InventorySearchBudget
from infra.db.inventory_search_readiness import InventorySearchReadinessReader
from infra.db.inventory_service_plan import InventoryServicePlanBudget
from infra.db.inventory_sources import InventorySources
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.search_quota import PostgresSearchQuotaRepository
from infra.model_api import ModelKeyFileResolver
from infra.standalone.settings import deployment_verification
from infra.tenant_model import resolve_task_model
from shared.schemas.identifiers import RunId, TenantId, new_id
from shared.schemas.search_backend import InventorySearchPolicy, SearchBackendPin
from tool_gateway.handlers.deepseek_search import DeepSeekSearchReaderFactory
from workflows.enterprise_knowledge.confirmed_context import ConfirmedContextReader

from .inventory_acquisition import InventoryAcquisitionDriver
from .inventory_collector import CodexInventoryCollector
from .inventory_model import (
    InventoryDiscoveryModel,
    InventoryEmployeeAuthority,
    InventoryModelAuthority,
)
from .inventory_ports import InventoryRuntimePorts
from .inventory_web import InventoryWebTools, build_inventory_web
from .scheduled_inventory import ScheduledInventoryDriver


def build_inventory_driver(
    *,
    core,
    sessions: async_sessionmaker[AsyncSession],
    control_sessions: async_sessionmaker[AsyncSession],
    control_tenant: TenantId,
    tenant_id: TenantId,
    settings,
    resolver,
    fingerprints,
    research,
    owner: str,
    inventory_ports: InventoryRuntimePorts | None = None,
) -> ScheduledInventoryDriver:
    """构造不访问外部；所有来源动作由job lease及Gateway再次授权。"""
    now = lambda: datetime.now(UTC)
    service_budget = InventoryServicePlanBudget(sessions, tenant_id, now=now)
    catalog = PostgresPublicCatalog(control_sessions, control_tenant)
    backend = PostgresInventoryService(sessions, tenant_id, catalog, assignment_bridge=CustomerAssignmentBridge())
    authority = InventoryEmployeeAuthority(core.employee_scope)
    lookup = Actor("system:inventory-model", EmployeeScope.SYSTEM, "system")
    configuration_repository = SqlModelConfigurationRepository(
        sessions,
        fingerprints,
        deployment_verification=deployment_verification(settings),
        deployment_provider=settings.provider,
    )
    configuration = ModelConfigurationServiceImpl(
        configuration_repository,
        CurrentEmployeeIdentity(core.employee_scope, lookup),
        now=now,
    )
    context_reader = ConfirmedContextReader(
        EnterpriseKnowledgeServiceImpl(
            lambda tenant: SqlAlchemyKnowledgeUnitOfWork(sessions, tenant)
        )
    )
    model = InventoryDiscoveryModel(
        tenant_id=tenant_id,
        backend=backend,
        employees=core.employee_scope,
        configuration=configuration,
        settings=settings,
        resolver=resolver,
        sessions=sessions,
        fingerprints=fingerprints,
        lease_owner=owner,
        context_reader=context_reader,
    )
    pages = inventory_ports or research
    if pages is None:
        raise ValueError("公开页面端口未配置")
    policy = settings.inventory_search
    artifacts = RawArtifactStoreImpl(
        lambda tenant: SqlAlchemyArtifactUnitOfWork(sessions, tenant),  # type: ignore[arg-type]
        pages.object_transport,
        pages.maximum_artifact_bytes,
        now,
        new_id,
    )

    async def current_cap(job: AcquisitionJob) -> int | None:
        limits = await service_budget.limits(job)
        return limits["searches"] if limits else None

    async def deepseek(job: AcquisitionJob, source: str, guard: Callable[[], Awaitable[None]]) -> DeepSeekSearchReaderFactory:
        if policy is None or policy.backend_id != "deepseek" or not policy.paid_search_enabled:
            raise ValueError("独立搜索付费许可未开启")
        if job.lease_token is None:
            raise ValueError("搜索任务缺少租约")
        auth = InventoryModelAuthority(job=job, source_id=source,
            configuration_version=settings.configuration_version, backend=backend,
            employees=core.employee_scope, configuration=configuration, context_reader=context_reader)
        identity = await auth.identity()
        await auth.check(identity)
        selected = resolve_task_model(settings, identity)
        if (selected.provider != "deepseek" or selected.api_base_url not in
            {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
            or selected.api_key_file is None or selected.api_connection_version is None
            or selected.api_control_tenant is None):
            raise ValueError("未选择官方搜索连接")
        pin = SearchBackendPin(backend_id="deepseek", connection_version=selected.api_connection_version,
            policy_version=policy.version)
        limits = await service_budget.limits(job)
        budget = InventorySearchBudget(sessions, tenant_id, job.id, job.lease_token, pin, policy, now=now,
            trusted_maximum_requests=min(limits["searches"], policy.maximum_requests) if limits else None,
            trusted_cap_reader=lambda: current_cap(job))
        await budget.bind()
        async def current_guard() -> None:
            await guard()
            await auth.check(identity)
            current = resolve_task_model(settings, identity)
            if current.api_connection_version != pin.connection_version or current.api_base_url != selected.api_base_url:
                raise ValueError("搜索连接已经改变")
        connector = DeepSeekSearchConnector(DeepSeekSearchApiTransport(),
            ModelKeyFileResolver(selected.api_key_file, selected.api_control_tenant, selected.api_connection_version, "MODEL_API_KEY"),
            base_url=selected.api_base_url)
        return DeepSeekSearchReaderFactory(tenant_id, RunId(str(job.id)), source, connector, budget, current_guard)

    async def tavily_budget(job: AcquisitionJob) -> InventorySearchBudget:
        if job.lease_token is None:
            raise ValueError("搜索任务缺少租约")
        effective = policy or InventorySearchPolicy(version=settings.configuration_version, backend_id="tavily",maximum_requests=24)
        pin = SearchBackendPin(backend_id="tavily",connection_version=settings.configuration_version,policy_version=effective.version)
        limits = await service_budget.limits(job)
        budget = InventorySearchBudget(sessions,tenant_id,job.id,job.lease_token,pin,effective,now=now,
            trusted_maximum_requests=min(limits["searches"], effective.maximum_requests) if limits else None,
            trusted_cap_reader=lambda: current_cap(job))
        await budget.bind()
        return budget

    async def tools(job: AcquisitionJob, source: str, guard: Callable[[], Awaitable[None]]) -> InventoryWebTools:
        actor = await authority.actor(tenant_id, job.employee_id)
        bound = await deepseek(job, source, guard) if policy is not None and policy.backend_id == "deepseek" else None
        if bound is None and research is None:
            raise ValueError("显式搜索后端不可用")
        return build_inventory_web(
            factory=sessions,
            tenant_id=tenant_id,
            run_id=RunId(str(job.id)),
            user_id=actor.user_id,
            country=(job.query.target_countries or ("ZZ",))[0]
            if (
                job.query.regions
                or job.query.countries
                or job.query.region not in {None, "africa"}
            )
            else job.query.country or "ZZ",
            countries=job.query.target_countries
            if (
                job.query.regions
                or job.query.countries
                or job.query.region not in {None, "africa"}
            )
            else (),
            category=job.query.product_tags[0],
            guard=guard,
            fingerprints=fingerprints,
            search_transport=research.search_transport if bound is None and research is not None else None,
            page_transport=pages.page_transport,
            secret_resolver=research.secret_resolver if research is not None else None,
            secret_ref=research.secret_ref if research is not None else "",
            search_factory=bound, maximum_query_chars=128 if bound else 400,
            search_reservation=None if bound else await tavily_budget(job), source_id=source,
            artifacts=artifacts,
            now=now,
        )

    async def probe(job: AcquisitionJob, guard: Callable[[], Awaitable[None]]) -> None:
        if policy is not None and policy.backend_id == "deepseek":
            await deepseek(job, "company_websites", guard)
        else:
            await tavily_budget(job)
            await (await tools(job, "company_websites", guard)).observe_usage()

    sources = InventorySources(
        tenant_id=tenant_id,
        backend=backend,
        model_snapshot=configuration_repository.get,
        quota=PostgresSearchQuotaRepository(sessions, tenant_id, now=now),
        configured=research is not None and research.exclusive_account_confirmed,
        search_policy=policy,
        search_readiness=InventorySearchReadinessReader(sessions, tenant_id, settings).read,
        bind_search=tavily_budget if research is not None and (policy is None or policy.backend_id == "tavily") else None,
        probe=probe,
    )
    driver = InventoryAcquisitionDriver(
        tenant_id=tenant_id,
        backend=backend,
        catalog=catalog,
        authority=authority,
        sources=sources,
        fit_model=model,
        context_reader=context_reader,
        service_budget=service_budget,
        collector=CodexInventoryCollector(
            tools, model.generate, context_reader=context_reader, service_budget=service_budget
        ),
        now=now,
    )

    return ScheduledInventoryDriver(driver, tenant_id=tenant_id, store=service_budget.store,
        requests=backend, receipts=service_budget, authority=authority, context=context_reader, now=now)
