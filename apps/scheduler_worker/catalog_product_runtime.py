"""目录产品调度的专用领域服务、workflow 与 outbox 组合。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.composition_support.outreach_fact_readers import ProspectingDemandAccountNames
from domains.approvals.service import ApprovalService
from domains.demand.service import DemandService
from domains.demand.service_impl import DemandServiceImpl
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.permissions import Phase2ProductAuthorizer
from domains.products.service import (
    CatalogProposalService,
    ProductActor,
    ProductRole,
)
from domains.prospecting.service import ProspectingService
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.catalog_reconciliation_checkpoints import (
    PostgresCatalogReconciliationCheckpointStore,
)
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from shared.errors import ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import (
    AccountCountryFactsChanged,
    ApprovalDecided,
    CatalogProductProposalCreated,
    CatalogProposalPolicyActivated,
    DomainEvent,
    NeedCatalogFactsChanged,
    NeedClusterMembershipChanged,
)
from shared.schemas.identifiers import TenantId
from workflows.catalog_product_proposal import (
    CatalogCultivationApprovalDecidedHandler,
    CatalogPolicyApprovalDecidedHandler,
    CatalogProductApplication,
    build_catalog_evaluation_workflow_definition,
    build_catalog_evaluation_workflow_handlers,
    build_catalog_policy_workflow_definition,
    build_catalog_policy_workflow_handlers,
    build_catalog_product_workflow_definition,
    build_catalog_product_workflow_handlers,
)
from workflows.catalog_product_proposal.account_facts import (
    ProspectingDemandCatalogAccountFactsReader,
)
from workflows.engine.runner import StepHandler, WorkflowEngine

from .catalog_products import (
    CatalogProductDriver,
    CatalogReconciliationCheckpointStore,
)


class CatalogOutboxRegistry(Protocol):
    """为目录事件注册具名 durable consumer 的窄接口。"""

    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None: ...


class _MembershipHandler:
    def __init__(self, application: CatalogProductApplication) -> None:
        self._application = application

    async def handle(self, event: NeedClusterMembershipChanged) -> None:
        await self._application.handle_need_cluster_membership_changed(event)


class _NeedFactsHandler:
    def __init__(self, application: CatalogProductApplication) -> None:
        self._application = application

    async def handle(self, event: NeedCatalogFactsChanged) -> None:
        await self._application.handle_need_catalog_facts_changed(event)


class _AccountFactsHandler:
    def __init__(
        self, application: CatalogProductApplication, batch_limit: int
    ) -> None:
        self._application = application
        self._batch_limit = batch_limit

    async def handle(self, event: AccountCountryFactsChanged) -> None:
        await self._application.handle_account_country_facts_changed(
            event, limit=self._batch_limit
        )


class _PolicyActivatedHandler:
    def __init__(
        self, application: CatalogProductApplication, batch_limit: int
    ) -> None:
        self._application = application
        self._batch_limit = batch_limit

    async def handle(self, event: CatalogProposalPolicyActivated) -> None:
        await self._application.handle_catalog_policy_activated(
            event, limit=self._batch_limit
        )


class _ProposalCreatedHandler:
    def __init__(self, application: CatalogProductApplication) -> None:
        self._application = application

    async def handle(self, event: CatalogProductProposalCreated) -> None:
        await self._application.handle_catalog_product_proposal_created(event)


@dataclass(frozen=True)
class CatalogProductRuntime:
    """已绑定共享 engine/outbox 的目录应用与扫描驱动。"""

    application: CatalogProductApplication
    driver: CatalogProductDriver


@dataclass(frozen=True)
class CatalogProductComposition:
    """先构造步骤 handler，再绑定同一 engine/outbox 的两阶段组合。"""

    demand: DemandService
    products: CatalogProposalService
    approvals: ApprovalService
    system_actor: ProductActor
    tenant_id: TenantId
    checkpoints: CatalogReconciliationCheckpointStore
    handlers: Mapping[str, StepHandler]

    def bind(
        self,
        *,
        engine: WorkflowEngine,
        outbox: CatalogOutboxRegistry,
        batch_limit: int,
    ) -> CatalogProductRuntime:
        if type(batch_limit) is not int or not 1 <= batch_limit <= 200:
            raise ValidationError("Catalog 调度批量上限无效")
        engine.register(build_catalog_policy_workflow_definition())
        engine.register(build_catalog_evaluation_workflow_definition())
        engine.register(build_catalog_product_workflow_definition())
        application = CatalogProductApplication(
            self.demand, self.products, engine, self.system_actor
        )

        def register(
            event_type: type[DomainEvent],
            name: str,
            handler: object,
        ) -> None:
            outbox.register_handler(
                event_type,
                name,
                cast(EventHandler[DomainEvent], handler),
            )

        register(
            NeedClusterMembershipChanged,
            "catalog_products.need_cluster_membership_changed",
            _MembershipHandler(application),
        )
        register(
            NeedCatalogFactsChanged,
            "catalog_products.need_catalog_facts_changed",
            _NeedFactsHandler(application),
        )
        register(
            AccountCountryFactsChanged,
            "catalog_products.account_country_facts_changed",
            _AccountFactsHandler(application, batch_limit),
        )
        register(
            CatalogProposalPolicyActivated,
            "catalog_products.policy_activated",
            _PolicyActivatedHandler(application, batch_limit),
        )
        register(
            CatalogProductProposalCreated,
            "catalog_products.proposal_created",
            _ProposalCreatedHandler(application),
        )
        register(
            ApprovalDecided,
            "catalog_product_policy.approval_decided",
            CatalogPolicyApprovalDecidedHandler(engine, self.approvals),
        )
        register(
            ApprovalDecided,
            "catalog_product_cultivation.approval_decided",
            CatalogCultivationApprovalDecidedHandler(engine, self.approvals),
        )
        return CatalogProductRuntime(
            application=application,
            driver=CatalogProductDriver(
                demand=self.demand,
                products=self.products,
                engine=engine,
                checkpoints=self.checkpoints,
                system_actor=self.system_actor,
                tenant_id=self.tenant_id,
                batch_limit=batch_limit,
            ),
        )


def build_catalog_product_composition(
    *,
    factory: async_sessionmaker[AsyncSession],
    prospecting: ProspectingService,
    approvals: ApprovalService,
    tenant_id: TenantId,
    now: Callable[[], datetime],
) -> CatalogProductComposition:
    """构造专用服务；构造本身不读库、不创建策略或任何供应动作。"""
    if (
        not callable(getattr(prospecting, "get_account", None))
        or not callable(now)
        or not isinstance(tenant_id, str)
        or not tenant_id.startswith("tn_")
        or tenant_id != tenant_id.strip()
        or len(tenant_id) > 40
    ):
        raise ValidationError("Catalog 运行时领域依赖无效")
    demand = cast(
        DemandService,
        DemandServiceImpl(
            lambda scoped: SqlAlchemyDemandUnitOfWork(  # type: ignore[arg-type, return-value]
                factory, scoped, now=now
            ),
            now=now,
            catalog_accounts=ProspectingDemandCatalogAccountFactsReader(prospecting),
            account_names=ProspectingDemandAccountNames(prospecting),
        ),
    )
    products = cast(
        CatalogProposalService,
        CatalogProposalServiceImpl(
            lambda scoped: SqlAlchemyCatalogProductsUnitOfWork(  # type: ignore[arg-type, return-value]
                factory, scoped
            ),
            Phase2ProductAuthorizer(tenant_id),
            now=now,
        ),
    )
    actor = ProductActor("system:catalog-products", ProductRole.SYSTEM, tenant_id)
    checkpoints = PostgresCatalogReconciliationCheckpointStore(factory, tenant_id)
    handlers = {
        **build_catalog_policy_workflow_handlers(products, approvals, actor, now=now),
        **build_catalog_evaluation_workflow_handlers(demand, products, actor),
        **build_catalog_product_workflow_handlers(
            demand, products, approvals, actor, now=now
        ),
    }
    return CatalogProductComposition(
        demand=demand,
        products=products,
        approvals=approvals,
        system_actor=actor,
        tenant_id=tenant_id,
        checkpoints=checkpoints,
        handlers=handlers,
    )


__all__ = (
    "CatalogProductComposition",
    "CatalogProductRuntime",
    "build_catalog_product_composition",
)
