"""独立Search的run绑定读取器，必须在Gateway检查后持久预留才解析Key。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from connectors.deepseek_search.client import DeepSeekSearchConnector
from connectors.web_search.client import WebSearchResult
from shared.schemas.identifiers import RunId, TenantId
from tool_gateway.handlers.web_search import RunBoundWebSearcherFactory


class SearchReader(Protocol):
    async def search(
        self, tenant_id: TenantId, query: str, country: str, limit: int
    ) -> tuple[WebSearchResult, ...]: ...


class SearchReservation(Protocol):
    async def reserve(self, source_id: str, request_key: str) -> None: ...
    async def finish(self, request_key: str) -> None: ...


@dataclass(frozen=True)
class DeepSeekSearchReaderFactory:
    tenant_id: TenantId
    run_id: RunId
    source_id: str
    connector: DeepSeekSearchConnector
    budget: SearchReservation
    guard: Callable[[], Awaitable[None]]

    def for_run(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        request_key: str,
        *,
        fingerprint_version: str,
    ) -> "BoundSearchReader":
        if (tenant_id, run_id) != (self.tenant_id, self.run_id):
            raise ValueError("搜索身份不匹配")
        return BoundSearchReader(self, request_key)


@dataclass(frozen=True)
class BoundSearchReader:
    factory: DeepSeekSearchReaderFactory
    request_key: str

    async def search(
        self, tenant_id: TenantId, query: str, country: str, limit: int
    ) -> tuple[WebSearchResult, ...]:
        if tenant_id != self.factory.tenant_id:
            raise ValueError("搜索企业不匹配")
        await self.factory.guard()
        await self.factory.budget.reserve(self.factory.source_id, self.request_key)
        results = await self.factory.connector.search(
            query, country=country, limit=limit
        )
        await self.factory.guard()
        await self.factory.budget.finish(self.request_key)
        return tuple(WebSearchResult(r.title, r.url, r.description) for r in results)


@dataclass(frozen=True)
class ReservedSearchReaderFactory:
    """为原免费供应商增加任务总次数预留，不更改其免费额度判断。"""

    delegate: RunBoundWebSearcherFactory
    budget: SearchReservation
    source_id: str

    def for_run(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        request_key: str,
        *,
        fingerprint_version: str,
    ) -> SearchReader:
        reader = self.delegate.for_run(
            tenant_id, run_id, request_key, fingerprint_version=fingerprint_version
        )
        budget, source_id = self.budget, self.source_id

        class Reader:
            async def search(
                self, tenant_id: TenantId, query: str, country: str, limit: int
            ) -> tuple[WebSearchResult, ...]:
                await budget.reserve(source_id, request_key)
                result = await reader.search(tenant_id, query, country, limit)
                await budget.finish(request_key)
                return result

        return Reader()
