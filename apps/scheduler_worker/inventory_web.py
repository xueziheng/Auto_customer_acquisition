"""公开库存专用Gateway装配；不复用或放宽旧Workflow/Campaign授权。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import RawArtifactStore
from connectors.playwright.public_page import BrowserPublicPageReader
from connectors.tavily.transport import TavilySearchTransport
from connectors.web_search.client import (
    PageSnapshot,
    WebSearchConnector,
    WebSearchResult,
    WebSearchSecretResolver,
)
from connectors.web_search.transport import PublicPageTransport
from infra.db.search_quota import PostgresSearchQuotaRepository
from infra.db.tables import ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.checks.web_discovery import WebResearchPreflight
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.deepseek_search import (
    ReservedSearchReaderFactory,
    SearchReservation,
)
from tool_gateway.handlers.free_search import MANIFEST as SEARCH_MANIFEST
from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher
from tool_gateway.handlers.inventory_usage import USAGE_MANIFEST, InventoryUsageHandler
from tool_gateway.handlers.web_read_page import MANIFEST as PAGE_MANIFEST
from tool_gateway.handlers.web_read_page import (
    ConnectorPublicPageReader,
    ToolGatewayWebPageReader,
    WebReadPageHandler,
)
from tool_gateway.handlers.web_search import (
    RunBoundWebSearcherFactory,
    ToolGatewayWebSearcher,
    WebSearchHandler,
)
from tool_gateway.handlers.web_slots import (
    SearchResultBatch,
    WebPageFailureSlot,
    WebPageSnapshotSlot,
    WebSearchResultSlot,
)
from tool_gateway.manifest import CostClass, ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.demand_discovery.ports import (
    WebDiscoveryPageReader,
)

from .free_web_discovery import DisabledPageSearchTransport, build_free_search_reader


class InventorySearcher(Protocol):
    """供应商无关的库存搜索端口，显式携带持久请求身份。"""

    async def search(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        query: str,
        country: str,
        category: str,
        limit: int,
        *,
        quota_request_key: str | None = None,
    ) -> SearchResultBatch: ...
    def release(self, batch: SearchResultBatch) -> None: ...


@dataclass(frozen=True)
class InventoryWebTools:
    searcher: InventorySearcher
    page_reader: WebDiscoveryPageReader
    browser_reader: WebDiscoveryPageReader
    observe_usage: Callable[[], Awaitable[None]]
    archive_candidate: Callable[[], Awaitable[PageSnapshot]] | None = None
    maximum_query_chars: int = 400


class BrowserGateway:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    async def invoke(self, ctx: ToolCallContext):
        return await self.gateway.invoke(replace(ctx, tool_id="web.browse_page"))


class InventoryWebGate:
    """固定本job身份和公开查询；guard每次核对lease与在职员工。"""

    name = "tenant"

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        run_id: RunId,
        user_id: UserId,
        country: str,
        category: str,
        countries: tuple[str, ...] = (),
        search_slot: WebSearchResultSlot | None = None,
        guard: Callable[[], Awaitable[None]],
    ) -> None:
        self.tenant_id, self.run_id, self.user_id = tenant_id, run_id, user_id
        self.country, self.category, self.guard = country, category, guard
        self.countries = countries or (country,)
        self.search_slot = search_slot

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if (ctx.tenant_id, ctx.run_id, ctx.user_id) != (
            self.tenant_id,
            self.run_id,
            self.user_id,
        ):
            return CheckRejection(self.name, "tenant:inventory", "库存采集身份不匹配")
        await self.guard()
        selected_country = ctx.params.get("country", self.country)
        if self.search_slot is not None and "search_result_handle" in ctx.params:
            batch = self.search_slot.get_batch(ctx.params["search_result_handle"])
            if batch.tenant_id != self.tenant_id:
                return CheckRejection(
                    self.name, "tenant:inventory_query", "库存查询不匹配"
                )
            selected_country = batch.country
        if (
            selected_country not in self.countries
            or ctx.params.get("category", self.category) != self.category
        ):
            return CheckRejection(self.name, "tenant:inventory_query", "库存查询不匹配")
        state.preflight = WebResearchPreflight(
            self.tenant_id, selected_country, self.category
        )
        return None


class InventoryBudget:
    name = "rate_limit"

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        guard: Callable[[], Awaitable[None]],
    ) -> None:
        self.factory, self.guard = factory, guard

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        await self.guard()
        async with self.factory() as session:
            used = await session.scalar(
                select(func.count())
                .select_from(ToolCallRow)
                .where(
                    ToolCallRow.tenant_id == ctx.tenant_id,
                    ToolCallRow.run_id == ctx.run_id,
                    ToolCallRow.tool_id.in_(
                        (
                            "web.search",
                            "web.read_page",
                            "web.browse_page",
                            "inventory.search_usage",
                        )
                    ),
                    ToolCallRow.status.notin_(("rejected", "duplicate")),
                )
            )
        if used is None or used > 24:
            return CheckRejection(
                self.name, "rate_limit:inventory", "库存任务外部请求预算耗尽"
            )
        return None


def build_inventory_web(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    run_id: RunId,
    user_id: UserId,
    country: str,
    category: str,
    guard: Callable[[], Awaitable[None]],
    fingerprints: HmacFingerprintProvider,
    search_transport: TavilySearchTransport | None = None,
    page_transport: PublicPageTransport,
    secret_resolver: WebSearchSecretResolver | None = None,
    secret_ref: str = "",
    artifacts: RawArtifactStore,
    now: Callable[[], datetime],
    browser: bool = False,
    countries: tuple[str, ...] = (),
    candidate_url: str | None = None,
    search_factory: RunBoundWebSearcherFactory | None = None,
    maximum_query_chars: int = 400,
    search_reservation: SearchReservation | None = None,
    source_id: str = "",
) -> InventoryWebTools:
    """每job/source独立Gateway和槽；只接受公开读取，与联系策略保持分离。"""
    search_slot, page_slot = (
        WebSearchResultSlot(new_id, maximum_batches=4),
        WebPageSnapshotSlot(new_id),
    )
    page_failures, browser_failures = WebPageFailureSlot(), WebPageFailureSlot()
    registry = ToolRegistry()
    free = None
    if (
        search_factory is None
        and search_transport is not None
        and secret_resolver is not None
    ):
        free = build_free_search_reader(
            factory=factory,
            tenant_id=tenant_id,
            search_transport=search_transport,
            page_transport=page_transport,
            secret_resolver=secret_resolver,
            secret_ref=secret_ref,
            now=now,
        )
        quota = PostgresSearchQuotaRepository(factory, tenant_id, now=now)
        registry.register(
            USAGE_MANIFEST,
            InventoryUsageHandler(free, fingerprints, quota.observe_usage),
        )
    bound_search = search_factory or free
    if bound_search is not None and search_reservation is not None:
        bound_search = ReservedSearchReaderFactory(
            bound_search, search_reservation, source_id
        )
    if bound_search is not None:
        registry.register(
            replace(
                SEARCH_MANIFEST,
                version="v1.inventory.deepseek" if search_factory else "v1.inventory",
                cost_class=CostClass.MEDIUM
                if search_factory
                else SEARCH_MANIFEST.cost_class,
                checks=("tenant", "permission", "rate_limit"),
            ),
            WebSearchHandler(
                None, search_slot, fingerprints, reader_factory=bound_search
            ),
        )
    reader = (
        BrowserPublicPageReader(artifacts)
        if browser
        else ConnectorPublicPageReader(
            lambda tenant: WebSearchConnector(
                DisabledPageSearchTransport(), page_transport, artifacts, now=now
            )
        )
    )
    registry.register(
        replace(
            PAGE_MANIFEST,
            version="v1.inventory.browser" if browser else "v1.inventory",
            checks=("tenant", "permission", "rate_limit"),
        ),
        WebReadPageHandler(
            reader, search_slot, page_slot, fingerprints, failure_slot=page_failures
        ),
    )
    registry.register(
        replace(
            PAGE_MANIFEST,
            tool_id="web.browse_page",
            version="v1.inventory.browser",
            checks=("tenant", "permission", "rate_limit"),
        ),
        WebReadPageHandler(
            BrowserPublicPageReader(artifacts),
            search_slot,
            page_slot,
            fingerprints,
            failure_slot=browser_failures,
        ),
    )

    async def authorize(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
        await guard()
        return (
            ctx.tenant_id == tenant_id
            and ctx.run_id == run_id
            and ctx.user_id == user_id
        )

    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]
        {
            "tenant": InventoryWebGate(
                tenant_id=tenant_id,
                run_id=run_id,
                user_id=user_id,
                country=country,
                countries=countries,
                search_slot=search_slot,
                category=category,
                guard=guard,
            ),
            "permission": PermissionCheck(authorize),
            "rate_limit": InventoryBudget(factory, guard),
        },
        lambda tenant: cast(
            ToolGatewayUnitOfWork,
            SqlAlchemyToolGatewayUnitOfWork(factory, tenant, now=now),
        ),
        lease_duration=timedelta(minutes=2),
        lease_owner="inventory-web",
        now=now,
        id_factory=new_id,
    )

    async def observe_usage() -> None:
        if free is None:
            raise ValueError("当前后端不提供免费额度检查")
        result = await gateway.invoke(
            ToolCallContext(
                tenant_id, user_id, USAGE_MANIFEST.tool_id, {}, run_id=run_id
            )
        )
        if result.status is not ToolCallStatus.SUCCEEDED:
            raise ValueError("免费搜索用量无法核实")

    page_reader = ToolGatewayWebPageReader(
        gateway, page_slot, user_id, failure_slot=page_failures
    )

    async def archive_candidate() -> PageSnapshot:
        # 仅受信准备工厂传入已分配候选URL，不向模型提供任意URL读取工具。
        if candidate_url is None:
            raise ValueError("准备任务未绑定来源页面")
        await guard()
        batch = search_slot.put(
            tenant_id,
            country,
            category,
            (WebSearchResult("已取得企业来源", candidate_url, ""),),
        )
        result = await page_reader.read_page(tenant_id, run_id, batch, 0)
        await guard()
        return result

    return InventoryWebTools(
        FreeSearchGatewaySearcher(
            ToolGatewayWebSearcher(gateway, search_slot, user_id), free.quota, tenant_id
        )
        if free is not None
        else ToolGatewayWebSearcher(gateway, search_slot, user_id),
        ToolGatewayWebPageReader(
            gateway, page_slot, user_id, failure_slot=page_failures
        ),
        ToolGatewayWebPageReader(
            BrowserGateway(gateway), page_slot, user_id, failure_slot=browser_failures
        ),
        observe_usage,
        archive_candidate if candidate_url is not None else None,
        maximum_query_chars,
    )
