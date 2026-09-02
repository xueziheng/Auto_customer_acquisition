"""Sourcing Case V2 的生产组合、可信读取边界与完整事件接线。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.sourcing_agent import (
    SourcingPageExtractionModelPort,
    SourcingPageExtractor,
)
from apps.scheduler_worker.config import SourcingSettings
from apps.scheduler_worker.free_web_discovery import (
    DisabledPageSearchTransport,
    build_free_search_reader,
)
from apps.scheduler_worker.sourcing_costing import SourcingCostHandoffHandler
from apps.scheduler_worker.sourcing_events import (
    DemandPriorityFactsReader,
    SourcingClusterMembershipHandler,
    SourcingTriggerHandler,
)
from apps.scheduler_worker.sourcing_projections import SourcingCandidateProductProjector
from apps.scheduler_worker.sourcing_web import (
    PostgresPublicCandidateDraftWriter,
    PostgresSourcingWebPersistence,
)
from artifact_store.store import RawArtifactStore
from connectors.tavily.transport import TavilySearchTransport
from connectors.web_search.client import WebSearchConnector, WebSearchSecretResolver
from connectors.web_search.transport import (
    PublicPageRejectedError,
    PublicPageTransport,
    canonical_public_page_url,
)
from domains.costing.permissions import (
    CostingActor,
    CostingScope,
    Phase1CostingAuthorizer,
)
from domains.costing.service import CostingService
from domains.costing.service_impl import CostingServiceImpl
from domains.demand.service_impl import DemandServiceImpl
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
    ScopeLevel,
)
from domains.opportunities.service import OpportunityService
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.service import ProductService
from domains.products.service_impl import ProductServiceImpl
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.schemas import (
    NeedFact,
    SourcingNeedSnapshot,
    canonical_sourcing_need_snapshot_hash,
)
from domains.sourcing.service import (
    CandidateEvidenceSnapshot,
    PublicSourcingPlan,
    SourcingService,
)
from domains.sourcing.service_impl import SourcingServiceImpl
from domains.suppliers.service import (
    Phase2SupplierAuthorizer,
    SupplierActor,
    SupplierQuoteEvidence,
    SupplierRole,
    SupplierService,
)
from domains.suppliers.service_impl import SupplierServiceImpl
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.products_uow import SqlAlchemyProductsUnitOfWork
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.suppliers_uow import SqlAlchemySuppliersUnitOfWork
from infra.db.tables import (
    RawArtifactRow,
    SourcingCandidateDraftRow,
    SourcingCandidateEvidenceRow,
    SourcingCaseRow,
    SourcingPublicPlanRow,
    ToolCallRow,
    WorkflowRunRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import ValidationError, detached_dependency_error
from shared.events.catalog import (
    DomainEvent,
    NeedBecameSourcingReady,
    NeedClusterMembershipChanged,
    NeedValidated,
    OpportunityQualified,
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
    SourcingCaseHandedToCosting,
    SourcingCaseOpened,
)
from shared.schemas.identifiers import (
    ArtifactId,
    RunId,
    TenantId,
    UserId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import FactualField, Provenance, summarize_provenance
from tool_gateway.checks.contact_provider import CountryPolicyDecisionReader
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.checks.web_discovery import (
    WebProviderRateLimitCheck,
    WebResearchCountryPolicyCheck,
    WebResearchPlaybookCheck,
    WebResearchPlaybookReader,
    WebResourceTenantCheck,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.free_search_contracts import SearchQuotaRepository
from tool_gateway.handlers.free_search import MANIFEST as FREE_SEARCH_MANIFEST
from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher
from tool_gateway.handlers.web_read_page import MANIFEST as WEB_READ_PAGE_MANIFEST
from tool_gateway.handlers.web_read_page import (
    ConnectorPublicPageReader,
    ToolGatewayWebPageReader,
    WebReadPageHandler,
)
from tool_gateway.handlers.web_search import ToolGatewayWebSearcher, WebSearchHandler
from tool_gateway.handlers.web_slots import WebPageSnapshotSlot, WebSearchResultSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckStage,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork, ToolGatewayUnitOfWorkFactory
from workflows.engine.runner import StepHandler, StepStatus, WorkflowEngine
from workflows.sourcing_case.flow import (
    build_sourcing_case_definition,
    build_sourcing_case_handlers,
)
from workflows.sourcing_case.ports import (
    AuthorizedPublicSourcingPlanReader,
    PersistedSearchReceiptPort,
    PublicCandidateDraftWriter,
    PublicPageReader,
    SourcingNeedReader,
)
from workflows.sourcing_case.steps import PublicSearchStep


class FailClosedSupplierQuoteEvidenceReader:
    """库内没有直接供应商报价事实表时，明确关闭 quoted 能力。"""

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> SupplierQuoteEvidence:
        del tenant_id, artifact_id
        raise ValidationError("直接供应商报价证据存储尚未装配") from None


class SourcingCandidatesReadyAuditAcknowledgement:
    """只确认最终事实已被本租户消费；没有业务写入或外部调用。"""

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    async def handle(self, event: object) -> None:
        if not isinstance(event, SourcingCandidatesReady):
            raise ValidationError("审计确认只接受 SourcingCandidatesReady")
        if event.tenant_id != self._tenant_id:
            return


class SourcingLifecycleAuditAcknowledgement:
    """确认本阶段已消费一个明确的寻源前置/交接事实。

    这是显式订阅而非全局 ``no-handler`` 豁免：Outbox 会为每个具名处理器留存
    durable delivery 记录。本确认不写业务状态、不调用外部端口，其他事件仍由
    Outbox 的 no-handler 失败关闭策略处理。
    """

    def __init__(
        self,
        tenant_id: TenantId,
        event_type: type[DomainEvent],
    ) -> None:
        self._tenant_id = tenant_id
        self._event_type = event_type

    async def handle(self, event: object) -> None:
        if type(event) is not self._event_type:
            raise ValidationError(f"审计确认只接受 {self._event_type.__name__}")
        if getattr(event, "tenant_id", None) != self._tenant_id:
            return


def _need_fact(value: FactualField[object]) -> NeedFact:
    provenance = cast(Provenance, value.provenance)
    return NeedFact(
        value=cast(str | int | date, value.value),
        provenance=summarize_provenance(provenance),
    )


class PostgresSourcingNeedReader:
    """从同租户 ValidatedNeed 实体逐字段构造不可补造的冻结快照。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id

    async def read(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingNeedSnapshot:
        if tenant_id != self._tenant_id:
            raise ValidationError("可信寻源需求租户绑定无效")
        dependency_error: Exception | None = None
        try:
            async with SqlAlchemyDemandUnitOfWork(self._factory, tenant_id) as uow:
                need = await uow.needs.get(tenant_id, need_id)
        except Exception as error:  # noqa: BLE001 -- Workflow 边界只接收常规依赖错误
            dependency_error = error
        if dependency_error is not None:
            raise detached_dependency_error(
                dependency_error,
                transient_message="可信寻源需求存储暂不可用",
                permanent_message="可信寻源需求事实不可用",
            ) from None
        if (
            need is None
            or need.tenant_id != tenant_id
            or need.need_id != need_id
            or getattr(need.status, "value", None)
            not in {
                "validated",
                "sourcing_ready",
                "handed_to_sourcing",
            }
            or need.quantity is None
            or need.completeness < 3
        ):
            raise ValidationError("可信寻源需求事实不可用")
        facts = {
            "product_category": _need_fact(
                cast(FactualField[object], need.product_category)
            ),
            "application": _need_fact(cast(FactualField[object], need.application))
            if need.application
            else None,
            "material": _need_fact(cast(FactualField[object], need.material))
            if need.material
            else None,
            "size_spec": _need_fact(cast(FactualField[object], need.size_spec))
            if need.size_spec
            else None,
            "model": None,
            "quantity": _need_fact(cast(FactualField[object], need.quantity)),
            "unit": _need_fact(cast(FactualField[object], need.unit))
            if need.unit
            else None,
            "destination": _need_fact(cast(FactualField[object], need.destination))
            if need.destination
            else None,
            "required_by": _need_fact(cast(FactualField[object], need.required_by))
            if need.required_by
            else None,
        }
        snapshot = SourcingNeedSnapshot(
            need_id=need_id,
            completeness=need.completeness,
            derivation_version="need-completeness-v1",
            snapshot_hash="0" * 64,
            product_category=cast(NeedFact, facts["product_category"]),
            application=cast(NeedFact | None, facts["application"]),
            material=cast(NeedFact | None, facts["material"]),
            size_spec=cast(NeedFact | None, facts["size_spec"]),
            model=None,
            quantity=cast(NeedFact, facts["quantity"]),
            unit=cast(NeedFact | None, facts["unit"]),
            destination=cast(NeedFact | None, facts["destination"]),
            required_by=cast(NeedFact | None, facts["required_by"]),
        )
        return snapshot.model_copy(
            update={
                "snapshot_hash": canonical_sourcing_need_snapshot_hash(snapshot)
            }
        )


def _safe_public_url(value: object) -> bool:
    """报告 URL 是否符合 Gateway 的纯公开页面形状规则。"""

    if not isinstance(value, str):
        return False
    try:
        canonical_public_page_url(value)
    except PublicPageRejectedError:
        return False
    return True


class PostgresCandidateEvidenceSnapshotReader:
    """核验同租户网页 Artifact 与候选草稿/候选证据的不可变绑定。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> CandidateEvidenceSnapshot:
        if tenant_id != self._tenant_id:
            raise ValidationError("候选网页证据租户绑定无效")
        dependency_error: Exception | None = None
        try:
            async with self._factory() as session:
                artifact = (
                    await session.execute(
                        select(RawArtifactRow).where(
                            RawArtifactRow.tenant_id == tenant_id,
                            RawArtifactRow.artifact_id == artifact_id,
                        )
                    )
                ).scalar_one_or_none()
                draft_bindings = (
                    await session.execute(
                        select(
                            SourcingCandidateDraftRow.evidence_url,
                            SourcingCandidateDraftRow.evidence_hash,
                            SourcingCandidateDraftRow.evidence_observed_at,
                        ).where(
                            SourcingCandidateDraftRow.tenant_id == tenant_id,
                            SourcingCandidateDraftRow.evidence_artifact_ref
                            == artifact_id,
                        )
                    )
                ).all()
                candidate_bindings = (
                    await session.execute(
                        select(
                            SourcingCandidateEvidenceRow.url,
                            SourcingCandidateEvidenceRow.content_hash,
                            SourcingCandidateEvidenceRow.observed_at,
                        ).where(
                            SourcingCandidateEvidenceRow.tenant_id == tenant_id,
                            SourcingCandidateEvidenceRow.artifact_id == artifact_id,
                        )
                    )
                ).all()
        except Exception as error:  # noqa: BLE001 -- Workflow 边界只接收常规依赖错误
            dependency_error = error
        if dependency_error is not None:
            raise detached_dependency_error(
                dependency_error,
                transient_message="候选网页证据存储暂不可用",
                permanent_message="候选网页证据不可验证",
            ) from None
        bindings = [*draft_bindings, *candidate_bindings]
        unique = {(row[0], row[1], row[2]) for row in bindings}
        if (
            artifact is None
            or artifact.kind != "web_snapshot"
            or artifact.mime_type != "text/html"
            or len(unique) != 1
        ):
            raise ValidationError("候选网页证据不可验证")
        url, content_hash, observed_at = unique.pop()
        try:
            canonical_url = canonical_public_page_url(url)
        except PublicPageRejectedError:
            canonical_url = None
        if (
            content_hash != artifact.content_hash
            or canonical_url is None
            or url != canonical_url
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise ValidationError("候选网页证据不可验证")
        return CandidateEvidenceSnapshot(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            canonical_url=canonical_url,
            content_hash=content_hash,
            observed_at=observed_at,
        )


@runtime_checkable
class BoundSourcingExtractionModelPort(SourcingPageExtractionModelPort, Protocol):
    """显式声明实际模型标识，供装配与部署配置做精确绑定。"""

    model_identifier: str


class DeploymentCeilingPlanReader:
    """在读取老板确认计划后再次施加部署硬上限，禁止配置被计划绕过。"""

    def __init__(
        self,
        delegate: AuthorizedPublicSourcingPlanReader,
        settings: SourcingSettings,
    ) -> None:
        self._delegate = delegate
        self._settings = settings

    async def load_authorized(self, **binding: object) -> PublicSourcingPlan:
        plan = await self._delegate.load_authorized(**binding)  # type: ignore[arg-type]
        if (
            not isinstance(plan, PublicSourcingPlan)
            or plan.provider != "tavily"
            or plan.search_depth != "basic"
            or plan.max_search_queries > self._settings.max_search_queries_per_plan
            or len(plan.queries) > self._settings.max_search_queries_per_plan
            or plan.max_pages_read > self._settings.max_pages_per_plan
        ):
            raise ValidationError("公开寻源计划超过部署安全上限")
        return plan


class PostgresSourcingWebRunTenantReader:
    """只承认同租户仍在 public_search 的 Sourcing Case V2 Run。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def owns_run(self, tenant_id: TenantId, run_id: RunId) -> bool:
        async with self._factory() as session:
            found = (
                await session.execute(
                    select(WorkflowRunRow.run_id)
                    .join(
                        SourcingCaseRow,
                        (SourcingCaseRow.tenant_id == WorkflowRunRow.tenant_id)
                        & (SourcingCaseRow.case_id == WorkflowRunRow.subject_ref),
                    )
                    .where(
                        WorkflowRunRow.tenant_id == tenant_id,
                        WorkflowRunRow.run_id == run_id,
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.workflow_version == 2,
                        WorkflowRunRow.current_step == "public_search",
                        WorkflowRunRow.status == StepStatus.RUNNING.value,
                        SourcingCaseRow.workflow_version == 2,
                    )
                )
            ).scalar_one_or_none()
        return found is not None


class PostgresSourcingGatewayBudget:
    """以授权计划、部署上限和 durable ToolCall 共同约束 Gateway 尝试。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: SourcingSettings,
    ) -> None:
        self._factory = factory
        self._settings = settings

    async def reserve(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        capability: str,
        now: datetime,
    ) -> int | None:
        if (
            capability not in {"web.search", "web.read_page"}
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() != UTC.utcoffset(now)
        ):
            raise ValidationError("公开寻源 Gateway 配额请求无效")
        async with self._factory() as session, session.begin():
            lock_key = int.from_bytes(
                sha256(
                    f"tradeos:sourcing-web-budget:v1:{tenant_id}:{run_id}".encode()
                ).digest()[:8],
                "big",
                signed=True,
            )
            await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
            bound = (
                await session.execute(
                    select(
                        SourcingPublicPlanRow.max_search_queries,
                        SourcingPublicPlanRow.max_pages_read,
                    )
                    .select_from(WorkflowRunRow)
                    .join(
                        SourcingCaseRow,
                        (SourcingCaseRow.tenant_id == WorkflowRunRow.tenant_id)
                        & (SourcingCaseRow.case_id == WorkflowRunRow.subject_ref),
                    )
                    .join(
                        SourcingPublicPlanRow,
                        (SourcingPublicPlanRow.tenant_id == SourcingCaseRow.tenant_id)
                        & (SourcingPublicPlanRow.case_id == SourcingCaseRow.case_id)
                        & (
                            SourcingPublicPlanRow.plan_id
                            == SourcingCaseRow.active_search_plan_id
                        ),
                    )
                    .where(
                        WorkflowRunRow.tenant_id == tenant_id,
                        WorkflowRunRow.run_id == run_id,
                        WorkflowRunRow.workflow_type == "sourcing_case",
                        WorkflowRunRow.workflow_version == 2,
                        WorkflowRunRow.current_step == "public_search",
                        WorkflowRunRow.status == StepStatus.RUNNING.value,
                        SourcingCaseRow.workflow_version == 2,
                        SourcingPublicPlanRow.status == "running",
                        SourcingPublicPlanRow.provider == "tavily",
                        SourcingPublicPlanRow.search_depth == "basic",
                    )
                )
            ).one_or_none()
            if bound is None:
                raise ValidationError("公开寻源 Gateway 计划绑定无效")
            plan_limit = (
                bound.max_search_queries
                if capability == "web.search"
                else bound.max_pages_read
            )
            deployment_limit = (
                self._settings.max_search_queries_per_plan
                if capability == "web.search"
                else self._settings.max_pages_per_plan
            )
            if type(plan_limit) is not int or not 1 <= plan_limit <= deployment_limit:
                raise ValidationError("公开寻源 Gateway 计划超过部署上限")
            attempts = (
                await session.execute(
                    select(func.count())
                    .select_from(ToolCallRow)
                    .where(
                        ToolCallRow.tenant_id == tenant_id,
                        ToolCallRow.run_id == run_id,
                        ToolCallRow.tool_id == capability,
                        ToolCallRow.status.notin_(("rejected", "duplicate")),
                    )
                )
            ).scalar_one()
            return None if attempts <= plan_limit else 86_400


@dataclass(frozen=True)
class SourcingResearchComposition:
    """生产外部依赖；仅 Tavily free、页面读取、Artifact 与显式模型端口。"""

    playbook: WebResearchPlaybookReader
    search_transport: TavilySearchTransport
    page_transport: PublicPageTransport
    artifacts: RawArtifactStore
    model_port: BoundSourcingExtractionModelPort

    def __post_init__(self) -> None:
        if (
            not isinstance(self.playbook, WebResearchPlaybookReader)
            or not isinstance(self.search_transport, TavilySearchTransport)
            or not isinstance(self.page_transport, PublicPageTransport)
            or not isinstance(self.artifacts, RawArtifactStore)
            or not isinstance(self.model_port, BoundSourcingExtractionModelPort)
        ):
            raise ValidationError("scheduler sourcing research 依赖未完整配置")


def build_sourcing_research_chain(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    settings: SourcingSettings,
    composition: SourcingResearchComposition,
    tool_user: UserId,
    fingerprints: HmacFingerprintProvider,
    secret_resolver: WebSearchSecretResolver,
    country_policy: CountryPolicyDecisionReader,
    lease_duration: timedelta,
    now: Callable[[], datetime],
) -> SourcingResearchChain:
    """构造 Tavily-free/Gateway-only research 链；构造期不解析 secret、不联网。"""
    if composition.model_port.model_identifier != settings.model_identifier:
        raise ValidationError("寻源模型标识与部署配置不一致")
    search_slot = WebSearchResultSlot(new_id, maximum_batches=100)
    page_slot = WebPageSnapshotSlot(new_id)
    free_reader = build_free_search_reader(
        factory=factory,
        tenant_id=tenant_id,
        search_transport=composition.search_transport,
        page_transport=composition.page_transport,
        secret_resolver=secret_resolver,
        secret_ref=settings.tavily_secret_ref,
        now=now,
    )

    def connector_factory(requested_tenant: TenantId) -> WebSearchConnector:
        if requested_tenant != tenant_id:
            raise ValidationError("公开寻源 connector 租户不匹配")
        return WebSearchConnector(
            DisabledPageSearchTransport(),
            composition.page_transport,
            composition.artifacts,
            now=now,
        )

    registry = ToolRegistry()
    registry.register(
        FREE_SEARCH_MANIFEST,
        WebSearchHandler(None, search_slot, fingerprints, reader_factory=free_reader),
    )
    registry.register(
        WEB_READ_PAGE_MANIFEST,
        WebReadPageHandler(
            ConnectorPublicPageReader(connector_factory),
            search_slot,
            page_slot,
            fingerprints,
        ),
    )
    if tuple(item.tool_id for item in registry.list_manifests()) != (
        "web.read_page",
        "web.search",
    ):
        raise ValidationError("公开寻源 Gateway registry 无效")

    async def authorize(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
        del state
        return (
            ctx.tenant_id == tenant_id
            and ctx.user_id == tool_user
            and ctx.tool_id in {"web.search", "web.read_page"}
        )

    checks: dict[str, CheckStage] = {
        "tenant": WebResourceTenantCheck(PostgresSourcingWebRunTenantReader(factory)),
        "permission": PermissionCheck(authorize),
        "playbook": WebResearchPlaybookCheck(composition.playbook, search_slot),
        "country_policy": WebResearchCountryPolicyCheck(country_policy),
        "rate_limit": WebProviderRateLimitCheck(
            PostgresSourcingGatewayBudget(factory, settings), now=now
        ),
    }

    def tool_uow(requested_tenant: TenantId) -> ToolGatewayUnitOfWork:
        return cast(
            ToolGatewayUnitOfWork,
            SqlAlchemyToolGatewayUnitOfWork(factory, requested_tenant, now=now),
        )

    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]
        checks,
        cast(ToolGatewayUnitOfWorkFactory, tool_uow),
        lease_duration=lease_duration,
        lease_owner="scheduler_sourcing_web",
        now=now,
        id_factory=new_id,
    )
    searcher = ToolGatewayWebSearcher(gateway, search_slot, tool_user)
    persistence = PostgresSourcingWebPersistence(
        factory, tenant_id, search_slot, now=now
    )
    return SourcingResearchChain(
        tavily_secret_ref=settings.tavily_secret_ref,
        model_port=composition.model_port,
        plan_reader=persistence,
        quota=free_reader.quota,
        searcher=FreeSearchGatewaySearcher(searcher, free_reader.quota, tenant_id),
        page_reader=ToolGatewayWebPageReader(gateway, page_slot, tool_user),
        receipts=persistence,
        drafts=PostgresPublicCandidateDraftWriter(factory, tenant_id, now=now),
    )


@dataclass(frozen=True)
class SourcingResearchChain:
    """已由 Tool Gateway 构造的 research-only Tavily/page 窄链。"""

    tavily_secret_ref: str = field(repr=False)
    model_port: BoundSourcingExtractionModelPort
    plan_reader: AuthorizedPublicSourcingPlanReader
    quota: SearchQuotaRepository
    searcher: FreeSearchGatewaySearcher
    page_reader: ToolGatewayWebPageReader
    receipts: PersistedSearchReceiptPort
    drafts: PublicCandidateDraftWriter


@dataclass(frozen=True)
class SourcingCaseComposition:
    """仅由真实 SQLAlchemy UoW/域服务构成的 Sourcing V2 组合结果。"""

    tenant_id: TenantId
    handlers: dict[str, StepHandler]
    demand: DemandPriorityFactsReader
    sourcing: SourcingService
    products: ProductService
    suppliers: SupplierService
    costing: CostingService
    need_reader: SourcingNeedReader
    sourcing_actor: SourcingActor
    product_actor: ProductActor
    supplier_actor: SupplierActor
    costing_actor: CostingActor

    def register(
        self,
        engine: WorkflowEngine,
        outbox: object,
    ) -> None:
        """注册完整且语义诚实的 V2 definition 与显式阶段订阅。"""
        engine.register(build_sourcing_case_definition())
        register = getattr(outbox, "register_handler", None)
        if not callable(register):
            raise ValidationError("寻源 Outbox registry 无效")
        trigger = SourcingTriggerHandler(
            sourcing=self.sourcing,
            demand=self.demand,
            need_reader=self.need_reader,
            tenant_id=self.tenant_id,
            sourcing_actor=self.sourcing_actor,
        )
        cluster_membership = SourcingClusterMembershipHandler(
            demand=self.demand,
            sourcing=self.sourcing,
            tenant_id=self.tenant_id,
            sourcing_actor=self.sourcing_actor,
        )
        projector = SourcingCandidateProductProjector(
            sourcing=self.sourcing,
            products=self.products,
            engine=engine,
            tenant_id=self.tenant_id,
            sourcing_actor=self.sourcing_actor,
            product_actor=self.product_actor,
        )
        ready = SourcingCandidatesReadyAuditAcknowledgement(self.tenant_id)
        case_opened = SourcingLifecycleAuditAcknowledgement(
            self.tenant_id, SourcingCaseOpened
        )
        opportunity_qualified = SourcingLifecycleAuditAcknowledgement(
            self.tenant_id, OpportunityQualified
        )
        costing = SourcingCostHandoffHandler(
            sourcing=self.sourcing,
            costing=self.costing,
            tenant_id=self.tenant_id,
            sourcing_actor=self.sourcing_actor,
            costing_actor=self.costing_actor,
        )
        register(NeedValidated, "sourcing_case.need_validated", trigger)
        register(NeedBecameSourcingReady, "sourcing_case.need_ready", trigger)
        register(
            NeedClusterMembershipChanged,
            "sourcing_case.cluster_membership",
            cluster_membership,
        )
        register(
            SourcingCandidatesVerified, "sourcing_case.product_projector", projector
        )
        register(SourcingCandidatesReady, "sourcing_case.ready_audit", ready)
        register(SourcingCaseHandedToCosting, "sourcing_case.costing_handoff", costing)
        register(
            SourcingCaseOpened,
            "sourcing_case.case_opened_audit",
            case_opened,
        )
        register(
            OpportunityQualified,
            "sourcing_case.opportunity_qualified_audit",
            opportunity_qualified,
        )


def build_sourcing_case_composition(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    settings: SourcingSettings,
    research: SourcingResearchChain,
    opportunities: OpportunityService,
    now: Callable[[], datetime],
) -> SourcingCaseComposition:
    """装配真实服务；外部研究端口必须是 Tavily-free/Gateway 具体链。"""
    if (
        not isinstance(research.model_port, BoundSourcingExtractionModelPort)
        or research.model_port.model_identifier != settings.model_identifier
        or research.tavily_secret_ref != settings.tavily_secret_ref
        or not isinstance(research.searcher, FreeSearchGatewaySearcher)
        or not isinstance(research.page_reader, ToolGatewayWebPageReader)
    ):
        raise ValidationError("寻源 research-only 依赖绑定无效")
    need_reader = PostgresSourcingNeedReader(factory, tenant_id)
    demand = DemandServiceImpl(
        cast(Any, lambda bound: SqlAlchemyDemandUnitOfWork(factory, bound)),
        now=now,
    )
    evidence_reader = PostgresCandidateEvidenceSnapshotReader(factory, tenant_id)
    sourcing_actor = SourcingActor(
        settings.system_actor_id, tenant_id, SourcingScope.SYSTEM, "system"
    )
    product_actor = ProductActor(
        settings.system_actor_id, ProductRole.SYSTEM, tenant_id
    )
    supplier_actor = SupplierActor(
        settings.system_actor_id, SupplierRole.SYSTEM, tenant_id
    )
    costing_actor = CostingActor(
        settings.system_actor_id, "system", CostingScope.SYSTEM, tenant_id
    )
    sourcing = SourcingServiceImpl(
        cast(Any, lambda bound: SqlAlchemySourcingUnitOfWork(factory, bound)),
        Phase2SourcingAuthorizer(tenant_id),
        evidence_reader,
        now=now,
    )
    products = ProductServiceImpl(
        cast(Any, lambda bound: SqlAlchemyProductsUnitOfWork(factory, bound)),
        Phase2ProductAuthorizer(tenant_id),
        now=now,
    )
    suppliers = SupplierServiceImpl(
        cast(Any, lambda bound: SqlAlchemySuppliersUnitOfWork(factory, bound)),
        Phase2SupplierAuthorizer(tenant_id),
        FailClosedSupplierQuoteEvidenceReader(),
        now=now,
    )
    costing = CostingServiceImpl(
        cast(Any, lambda bound: SqlAlchemyCostingUnitOfWork(factory, bound, now=now)),
        Phase1CostingAuthorizer(tenant_id),
        now=now,
    )
    public_search = PublicSearchStep(
        need_reader=need_reader,
        plan_reader=DeploymentCeilingPlanReader(research.plan_reader, settings),
        quota=research.quota,
        searcher=research.searcher,
        page_reader=cast(PublicPageReader, research.page_reader),
        receipts=research.receipts,
        extractor=SourcingPageExtractor(research.model_port),
        drafts=research.drafts,
    )
    opportunity_actor = OpportunityActor(
        settings.system_actor_id,
        OpportunityScope(level=ScopeLevel.SYSTEM),
        "system",
    )
    handlers = build_sourcing_case_handlers(
        need_reader=need_reader,
        products=products,
        suppliers=suppliers,
        sourcing=sourcing,
        product_actor=product_actor,
        supplier_actor=supplier_actor,
        sourcing_actor=sourcing_actor,
        opportunities=opportunities,
        opportunity_actor=opportunity_actor,
        public_search_handler=cast(StepHandler, public_search),
    )
    return SourcingCaseComposition(
        tenant_id,
        handlers,
        demand,
        sourcing,
        products,
        suppliers,
        costing,
        need_reader,
        sourcing_actor,
        product_actor,
        supplier_actor,
        costing_actor,
    )


__all__ = (
    "BoundSourcingExtractionModelPort",
    "DeploymentCeilingPlanReader",
    "FailClosedSupplierQuoteEvidenceReader",
    "PostgresCandidateEvidenceSnapshotReader",
    "PostgresSourcingGatewayBudget",
    "PostgresSourcingNeedReader",
    "PostgresSourcingWebRunTenantReader",
    "SourcingCandidatesReadyAuditAcknowledgement",
    "SourcingCaseComposition",
    "SourcingResearchChain",
    "SourcingResearchComposition",
    "build_sourcing_case_composition",
    "build_sourcing_research_chain",
)
