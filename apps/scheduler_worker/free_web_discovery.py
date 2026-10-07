"""Tavily 免费能力的部署组合；不回退 Brave，不在启动时触碰凭证或网络。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.tavily.client import TAVILY_API_KEY_REF, TavilySearchConnector
from connectors.tavily.transport import TavilySearchTransport
from connectors.web_search.client import WebSearchSecretResolver
from connectors.web_search.transport import PublicPageTransport, SearchHttpResponse
from infra.db.search_quota import PostgresSearchQuotaRepository
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.handlers.free_search import FreeSearchReaderFactory


class DisabledPageSearchTransport:
    """复用旧页面证据 connector 时显式禁用其搜索功能，绝不访问备用供应商。"""

    async def search(
        self, query: str, country: str, count: int, *, api_key: str
    ) -> SearchHttpResponse:
        raise ValidationError("页面读取器不允许搜索")


class _BoundTavilySecretResolver:
    def __init__(self, resolver: WebSearchSecretResolver, configured_ref: str) -> None:
        self._resolver = resolver
        self._configured_ref = configured_ref

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != TAVILY_API_KEY_REF:
            raise ValidationError("免费搜索凭证引用无效")
        return self._resolver.resolve(self._configured_ref)


def build_free_search_reader(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    search_transport: TavilySearchTransport,
    page_transport: PublicPageTransport,
    secret_resolver: WebSearchSecretResolver,
    secret_ref: str,
    now: Callable[[], datetime],
) -> FreeSearchReaderFactory:
    """显式受信部署绑定使用同一个持久账户槽，key 引用轮换绝不创建新额度。"""
    return FreeSearchReaderFactory(
        tenant_id=tenant_id,
        quota=PostgresSearchQuotaRepository(factory, tenant_id, now=now),
        connector_factory=lambda: TavilySearchConnector(
            search_transport, page_transport
        ),
        secret_resolver=_BoundTavilySecretResolver(secret_resolver, secret_ref),
    )
