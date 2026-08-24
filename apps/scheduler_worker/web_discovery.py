"""公开搜索工具的生产持久边界与 workflow-facing 组装。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import RawArtifactStore
from connectors.web_search.client import (
    WebSearchConnector,
    WebSearchSecretResolver,
)
from connectors.web_search.transport import BraveSearchTransport, PublicPageTransport
from infra.db.tables import ToolCallRow, WorkflowRunRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id
from tool_gateway.checks.contact_provider import CountryPolicyDecisionReader
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.checks.web_discovery import (
    WebProviderQuotaGuard,
    WebProviderRateLimitCheck,
    WebResearchCountryPolicyCheck,
    WebResearchPlaybookCheck,
    WebResearchPlaybookReader,
    WebResourceTenantCheck,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.web_read_page import (
    MANIFEST as WEB_READ_PAGE_MANIFEST,
)
from tool_gateway.handlers.web_read_page import (
    ConnectorPublicPageReader,
    ToolGatewayWebPageReader,
    WebReadPageHandler,
)
from tool_gateway.handlers.web_search import MANIFEST as WEB_SEARCH_MANIFEST
from tool_gateway.handlers.web_search import (
    ConnectorWebSearcher,
    ToolGatewayWebSearcher,
    WebSearchHandler,
)
from tool_gateway.handlers.web_slots import WebPageSnapshotSlot, WebSearchResultSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckStage,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import (
    ToolGatewayUnitOfWork,
    ToolGatewayUnitOfWorkFactory,
)
from workflows.demand_discovery.ports import (
    WebDiscoveryPageReader,
    WebDiscoverySearcher,
)
from workflows.engine.runner import StepStatus

_WORKFLOW_TYPE = "demand_discovery"
_BUDGETS = {
    WEB_SEARCH_MANIFEST.tool_id: ("query_budget", 100),
    WEB_READ_PAGE_MANIFEST.tool_id: ("page_budget", 50),
}


class _BoundWebSecretResolver:
    """把 connector 固定逻辑引用映射到部署提供的环境密钥引用。"""

    def __init__(
        self,
        resolver: WebSearchSecretResolver,
        configured_ref: str,
    ) -> None:
        if (
            not isinstance(resolver, WebSearchSecretResolver)
            or not isinstance(configured_ref, str)
            or not configured_ref
            or configured_ref != configured_ref.strip()
        ):
            raise ValidationError("公开搜索密钥引用配置无效")
        self._resolver = resolver
        self._configured_ref = configured_ref

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "WEB_SEARCH_API_KEY_REF":
            raise ValidationError("公开搜索凭证引用无效")
        return self._resolver.resolve(self._configured_ref)


class PostgresWebRunTenantReader:
    """只承认当前租户仍在运行的 demand_discovery run。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def owns_run(self, tenant_id: TenantId, run_id: RunId) -> bool:
        async with self._factory() as session:
            found = (
                await session.execute(
                    select(WorkflowRunRow.run_id).where(
                        WorkflowRunRow.tenant_id == str(tenant_id),
                        WorkflowRunRow.run_id == str(run_id),
                        WorkflowRunRow.workflow_type == _WORKFLOW_TYPE,
                        WorkflowRunRow.status == StepStatus.RUNNING.value,
                    )
                )
            ).scalar_one_or_none()
        return found is not None


class PostgresWebProviderQuotaGuard:
    """以已创建的 Tool ledger 行作为 durable 预算预留。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def reserve(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        capability: str,
        now: datetime,
    ) -> int | None:
        budget_spec = _BUDGETS.get(capability)
        if (
            budget_spec is None
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() != UTC.utcoffset(now)
        ):
            raise ValidationError("公开搜索配额请求无效")
        budget_key, maximum = budget_spec
        async with self._factory() as session, session.begin():
            run = (
                await session.execute(
                    select(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.tenant_id == str(tenant_id),
                        WorkflowRunRow.run_id == str(run_id),
                        WorkflowRunRow.workflow_type == _WORKFLOW_TYPE,
                        WorkflowRunRow.status == StepStatus.RUNNING.value,
                        WorkflowRunRow.current_step == "execute_search",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None or not isinstance(run.context, dict):
                raise ValidationError("公开搜索 workflow 配额上下文无效")
            budget = run.context.get(budget_key)
            if type(budget) is not int or not 1 <= budget <= maximum:
                raise ValidationError("公开搜索 workflow 配额无效")
            used = (
                await session.execute(
                    select(func.count())
                    .select_from(ToolCallRow)
                    .where(
                        ToolCallRow.tenant_id == str(tenant_id),
                        ToolCallRow.run_id == str(run_id),
                        ToolCallRow.tool_id == capability,
                        ToolCallRow.status.notin_(("rejected", "duplicate")),
                    )
                )
            ).scalar_one()
            # Gateway 已在进入检查管线前写入当前 received 行，因此 used==budget
            # 仍允许本次；下一次会得到 budget+1 并在任何 Provider IO 前阻断。
            return None if used <= budget else 86_400


@dataclass(frozen=True)
class WebDiscoveryToolComposition:
    """启用公开搜索所需的 Playbook、凭证、传输与证据存储。"""

    playbook: WebResearchPlaybookReader
    secret_resolver: WebSearchSecretResolver
    secret_ref: str
    search_transport: BraveSearchTransport
    page_transport: PublicPageTransport
    artifacts: RawArtifactStore

    def __post_init__(self) -> None:
        if (
            not isinstance(self.playbook, WebResearchPlaybookReader)
            or not isinstance(self.secret_resolver, WebSearchSecretResolver)
            or not isinstance(self.secret_ref, str)
            or not self.secret_ref
            or self.secret_ref != self.secret_ref.strip()
            or not isinstance(self.search_transport, BraveSearchTransport)
            or not isinstance(self.page_transport, PublicPageTransport)
            or not isinstance(self.artifacts, RawArtifactStore)
        ):
            raise ValidationError("scheduler 公开搜索工具依赖未完整配置")


@dataclass(frozen=True)
class WebDiscoveryTools:
    searcher: WebDiscoverySearcher
    page_reader: WebDiscoveryPageReader


def build_web_discovery_tools(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    tool_user: UserId,
    fingerprints: HmacFingerprintProvider,
    composition: WebDiscoveryToolComposition,
    country_policy: CountryPolicyDecisionReader,
    lease_duration: timedelta,
    now: Callable[[], datetime],
) -> WebDiscoveryTools:
    """注册双工具及全部 fail-closed 检查，返回 workflow 窄适配器。"""
    search_slot = WebSearchResultSlot(new_id, maximum_batches=100)
    page_slot = WebPageSnapshotSlot(new_id)
    bound_secrets = _BoundWebSecretResolver(
        composition.secret_resolver,
        composition.secret_ref,
    )

    def connector_factory(requested_tenant: TenantId) -> WebSearchConnector:
        if requested_tenant != tenant_id:
            raise ValidationError("公开搜索 connector 租户不匹配")
        return WebSearchConnector(
            composition.search_transport,
            composition.page_transport,
            composition.artifacts,
            now=now,
        )

    registry = ToolRegistry()
    registry.register(
        WEB_SEARCH_MANIFEST,
        WebSearchHandler(
            ConnectorWebSearcher(connector_factory, bound_secrets),
            search_slot,
            fingerprints,
        ),
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

    async def authorize(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
        del state
        return (
            ctx.tenant_id == tenant_id
            and ctx.user_id == tool_user
            and ctx.tool_id in _BUDGETS
        )

    run_reader = PostgresWebRunTenantReader(factory)
    quota: WebProviderQuotaGuard = PostgresWebProviderQuotaGuard(factory)
    checks: dict[str, CheckStage] = {
        "tenant": WebResourceTenantCheck(run_reader),
        "permission": PermissionCheck(authorize),
        "playbook": WebResearchPlaybookCheck(composition.playbook, search_slot),
        "country_policy": WebResearchCountryPolicyCheck(
            country_policy
        ),
        "rate_limit": WebProviderRateLimitCheck(quota, now=now),
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
        lease_owner="scheduler_web_discovery",
        now=now,
        id_factory=new_id,
    )
    return WebDiscoveryTools(
        ToolGatewayWebSearcher(gateway, search_slot, tool_user),
        ToolGatewayWebPageReader(gateway, page_slot, tool_user),
    )


__all__ = (
    "PostgresWebProviderQuotaGuard",
    "PostgresWebRunTenantReader",
    "WebDiscoveryToolComposition",
    "WebDiscoveryTools",
    "build_web_discovery_tools",
)
