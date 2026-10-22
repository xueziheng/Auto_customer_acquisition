"""公开库存专用Gateway装配；不复用或放宽旧Workflow/Campaign授权。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import RawArtifactStore
from connectors.playwright.public_page import BrowserPublicPageReader
from connectors.tavily.transport import TavilySearchTransport
from connectors.web_search.client import WebSearchConnector, WebSearchSecretResolver
from connectors.web_search.transport import PublicPageTransport
from infra.db.search_quota import PostgresSearchQuotaRepository
from infra.db.tables import ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.checks.web_discovery import WebResearchPreflight
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.free_search import MANIFEST as SEARCH_MANIFEST
from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher
from tool_gateway.handlers.inventory_usage import USAGE_MANIFEST, InventoryUsageHandler
from tool_gateway.handlers.web_read_page import MANIFEST as PAGE_MANIFEST
from tool_gateway.handlers.web_read_page import (
    ConnectorPublicPageReader,
    ToolGatewayWebPageReader,
    WebReadPageHandler,
)
from tool_gateway.handlers.web_search import ToolGatewayWebSearcher, WebSearchHandler
from tool_gateway.handlers.web_slots import (
    WebPageFailureSlot,
    WebPageSnapshotSlot,
    WebSearchResultSlot,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.demand_discovery.ports import (
    WebDiscoveryPageReader,
    WebDiscoverySearcher,
)

from .free_web_discovery import DisabledPageSearchTransport, build_free_search_reader


@dataclass(frozen=True)
class InventoryWebTools:
    searcher: WebDiscoverySearcher
    page_reader: WebDiscoveryPageReader
    browser_reader: WebDiscoveryPageReader
    observe_usage: Callable[[], Awaitable[None]]


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
        guard: Callable[[], Awaitable[None]],
    ) -> None:
        self.tenant_id, self.run_id, self.user_id = tenant_id, run_id, user_id
        self.country, self.category, self.guard = country, category, guard

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
        if (
            ctx.params.get("country", self.country) != self.country
            or ctx.params.get("category", self.category) != self.category
        ):
            return CheckRejection(self.name, "tenant:inventory_query", "库存查询不匹配")
        state.preflight = WebResearchPreflight(
            self.tenant_id, self.country, self.category
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
    search_transport: TavilySearchTransport,
    page_transport: PublicPageTransport,
    secret_resolver: WebSearchSecretResolver,
    secret_ref: str,
    artifacts: RawArtifactStore,
    now: Callable[[], datetime],
    browser: bool = False,
) -> InventoryWebTools:
    """每job/source独立Gateway和槽；只接受公开读取，与联系策略保持分离。"""
    search_slot, page_slot = (
        WebSearchResultSlot(new_id, maximum_batches=4),
        WebPageSnapshotSlot(new_id),
    )
    page_failures, browser_failures = WebPageFailureSlot(), WebPageFailureSlot()
    free = build_free_search_reader(
        factory=factory,
        tenant_id=tenant_id,
        search_transport=search_transport,
        page_transport=page_transport,
        secret_resolver=secret_resolver,
        secret_ref=secret_ref,
        now=now,
    )
    registry = ToolRegistry()
    quota = PostgresSearchQuotaRepository(factory, tenant_id, now=now)
    registry.register(
        USAGE_MANIFEST, InventoryUsageHandler(free, fingerprints, quota.observe_usage)
    )
    registry.register(
        replace(
            SEARCH_MANIFEST,
            version="v1.inventory",
            checks=("tenant", "permission", "rate_limit"),
        ),
        WebSearchHandler(None, search_slot, fingerprints, reader_factory=free),
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
        WebReadPageHandler(reader, search_slot, page_slot, fingerprints, failure_slot=page_failures),
    )
    registry.register(
        replace(
            PAGE_MANIFEST,
            tool_id="web.browse_page",
            version="v1.inventory.browser",
            checks=("tenant", "permission", "rate_limit"),
        ),
        WebReadPageHandler(
            BrowserPublicPageReader(artifacts), search_slot, page_slot, fingerprints, failure_slot=browser_failures
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
        result = await gateway.invoke(
            ToolCallContext(
                tenant_id, user_id, USAGE_MANIFEST.tool_id, {}, run_id=run_id
            )
        )
        if result.status is not ToolCallStatus.SUCCEEDED:
            raise ValueError("免费搜索用量无法核实")

    return InventoryWebTools(
        FreeSearchGatewaySearcher(
            ToolGatewayWebSearcher(gateway, search_slot, user_id), free.quota, tenant_id
        ),
        ToolGatewayWebPageReader(gateway, page_slot, user_id, failure_slot=page_failures),
        ToolGatewayWebPageReader(BrowserGateway(gateway), page_slot, user_id, failure_slot=browser_failures),
        observe_usage,
    )
