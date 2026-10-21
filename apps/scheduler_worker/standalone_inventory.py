"""独立后台库存机械装配；显式注入控制库、公司库和研究端口。"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from artifact_store.service_impl import RawArtifactStoreImpl
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import Actor, EmployeeScope
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.inventory_sources import InventorySources
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.search_quota import PostgresSearchQuotaRepository
from infra.standalone.settings import deployment_verification
from shared.schemas.identifiers import RunId, TenantId, new_id

from .inventory_acquisition import InventoryAcquisitionDriver
from .inventory_collector import CodexInventoryCollector
from .inventory_model import InventoryDiscoveryModel, InventoryEmployeeAuthority
from .inventory_web import build_inventory_web


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
) -> InventoryAcquisitionDriver:
    """构造不访问外部；所有来源动作由job lease及Gateway再次授权。"""
    now = lambda: datetime.now(UTC)
    catalog = PostgresPublicCatalog(control_sessions, control_tenant)
    backend = PostgresInventoryService(sessions, tenant_id, catalog)
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
    )
    artifacts = RawArtifactStoreImpl(
        lambda tenant: SqlAlchemyArtifactUnitOfWork(sessions, tenant),  # type: ignore[arg-type]
        research.object_transport,
        research.maximum_artifact_bytes,
        now,
        new_id,
    )

    async def tools(job, source, guard):
        actor = await authority.actor(tenant_id, job.employee_id)
        return build_inventory_web(
            factory=sessions,
            tenant_id=tenant_id,
            run_id=RunId(str(job.id)),
            user_id=actor.user_id,
            country=job.query.country or "ZZ",
            category=job.query.product_tags[0],
            guard=guard,
            fingerprints=fingerprints,
            search_transport=research.search_transport,
            page_transport=research.page_transport,
            secret_resolver=research.secret_resolver,
            secret_ref=research.secret_ref,
            artifacts=artifacts,
            now=now,
        )

    async def probe(job, guard):
        await (await tools(job, "company_websites", guard)).observe_usage()

    sources = InventorySources(
        tenant_id=tenant_id,
        backend=backend,
        model_snapshot=configuration_repository.get,
        quota=PostgresSearchQuotaRepository(sessions, tenant_id, now=now),
        configured=research.exclusive_account_confirmed,
        probe=probe,
    )
    return InventoryAcquisitionDriver(
        tenant_id=tenant_id,
        backend=backend,
        catalog=catalog,
        authority=authority,
        sources=sources,
        collector=CodexInventoryCollector(tools, model.generate),
        now=now,
    )
