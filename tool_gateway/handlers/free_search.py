"""Tavily 免费搜索插件：全部 Gateway 检查后才读 usage、预留与 dispatch。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import cast

from connectors.search_contracts import SearchResult
from connectors.tavily.client import TavilySearchConnector, TavilySecretResolver
from connectors.tavily.transport import TavilyRateLimitedError, TavilyTransientError
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.free_search_contracts import (
    FreeSearchError,
    FreeSearchStopReason,
    SearchQuotaRepository,
)
from tool_gateway.handlers.web_search import MANIFEST as WEB_SEARCH_MANIFEST
from tool_gateway.handlers.web_search import ToolGatewayWebSearcher
from tool_gateway.handlers.web_slots import SearchResultBatch
from tool_gateway.manifest import CostClass

MANIFEST = replace(
    WEB_SEARCH_MANIFEST,
    version="v1.free",
    cost_class=CostClass.FREE,
    input_schema={
        "type": "object",
        "required": ("query", "country", "category", "limit"),
        "properties": {
            **cast(dict[str, object], WEB_SEARCH_MANIFEST.input_schema["properties"]),
            "quota_request_key": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
        "additionalProperties": False,
    },
)


@dataclass(frozen=True, repr=False)
class FreeSearchReader:
    """不可变单 Run/操作绑定；任何已预留调用都没有退款或自动重试路径。"""

    tenant_id: TenantId
    run_id: RunId
    request_key: str
    fingerprint_version: str
    quota: SearchQuotaRepository = field(repr=False)
    connector_factory: Callable[[], TavilySearchConnector] = field(repr=False)
    secret_resolver: TavilySecretResolver = field(repr=False)

    async def search(
        self, tenant_id: TenantId, query: str, country: str, limit: int
    ) -> tuple[SearchResult, ...]:
        """失败只返回固定分类；dispatch 前提交 uncertain，成功才改 consumed。"""
        if tenant_id != self.tenant_id:
            raise ValidationError("免费搜索租户不匹配")
        try:
            await self.quota.check_available(
                self.run_id,
                self.request_key,
                fingerprint_version=self.fingerprint_version,
            )
            try:
                connector = self.connector_factory()
                await connector.configure(self.secret_resolver)
                usage = await connector.usage()
            except Exception:  # noqa: BLE001 - Provider/凭证异常只允许固定安全分类。
                await self.quota.record_unavailable(self.run_id)
                raise FreeSearchError(FreeSearchStopReason.USAGE_UNKNOWN) from None
            await self.quota.reserve(
                self.run_id,
                self.request_key,
                usage,
                fingerprint_version=self.fingerprint_version,
            )
            await self.quota.mark_dispatched(self.run_id, self.request_key)
            try:
                results = await connector.search(query, country=country, limit=limit)
            except TavilyRateLimitedError as error:
                raise ToolGatewayError(
                    ToolErrorCategory.RATE_LIMITED,
                    retry_after_seconds=error.retry_after_seconds,
                ) from None
            except TavilyTransientError:
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
            except Exception:  # noqa: BLE001 - dispatch 后任何故障均不得释放或重试。
                raise FreeSearchError(FreeSearchStopReason.REQUEST_UNCERTAIN) from None
            try:
                await self.quota.consume(self.run_id, self.request_key)
            except Exception:  # noqa: BLE001 - 成功结果但记账未知必须人工对账。
                raise FreeSearchError(FreeSearchStopReason.REQUEST_UNCERTAIN) from None
            return results
        except FreeSearchError as error:
            # ledger 的技术状态必须遵循其分类；业务禁止重试由外层 adapter 还原。
            raise ToolGatewayError(error.category) from None
        except ToolGatewayError:
            raise
        except Exception:  # noqa: BLE001 - DB 故障不得泄露连接信息。
            # DB 异常可能含参数或连接信息，不能传播到模型/ledger。
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED) from None


@dataclass(frozen=True, repr=False)
class FreeSearchReaderFactory:
    """由 worker 一次注入部署绑定，prepare 时只绑定 run，不触碰凭证或 IO。"""

    tenant_id: TenantId
    quota: SearchQuotaRepository
    connector_factory: Callable[[], TavilySearchConnector]
    secret_resolver: TavilySecretResolver

    def __post_init__(self) -> None:
        if (
            not self.tenant_id
            or not isinstance(self.quota, SearchQuotaRepository)
            or not callable(self.connector_factory)
            or not isinstance(self.secret_resolver, TavilySecretResolver)
        ):
            raise ValidationError("免费搜索部署绑定无效")

    def for_run(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        request_key: str,
        *,
        fingerprint_version: str,
    ) -> FreeSearchReader:
        """保持现有 `.search(tenant_id, query, country, limit)` reader 形状。"""
        if tenant_id != self.tenant_id or not run_id:
            raise ValidationError("免费搜索 Run 绑定无效")
        return FreeSearchReader(
            tenant_id,
            run_id,
            request_key,
            fingerprint_version,
            self.quota,
            self.connector_factory,
            self.secret_resolver,
        )


class FreeSearchGatewaySearcher:
    """复用 Gateway 全部检查与结果槽，把持久安全原因投影回 workflow。"""

    def __init__(
        self,
        delegate: ToolGatewayWebSearcher,
        quota: SearchQuotaRepository,
        tenant_id: TenantId,
    ) -> None:
        self._delegate = delegate
        self._quota = quota
        self._tenant_id = tenant_id

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
    ) -> SearchResultBatch:
        if tenant_id != self._tenant_id:
            raise ValidationError("免费搜索租户不匹配")
        try:
            if quota_request_key is None:
                return await self._delegate.search(
                    tenant_id, run_id, query, country, category, limit
                )
            return await self._delegate.search(
                tenant_id,
                run_id,
                query,
                country,
                category,
                limit,
                quota_request_key=quota_request_key,
            )
        except ToolGatewayError as error:
            if error.category not in {
                ToolErrorCategory.PROVIDER_PERMANENT,
                ToolErrorCategory.RECONCILIATION_REQUIRED,
            }:
                raise
            try:
                state = await self._quota.run_state(run_id)
            except Exception:  # noqa: BLE001 - 状态查询失败必须保守停止。
                raise FreeSearchError(FreeSearchStopReason.REQUEST_UNCERTAIN) from None
            if state is not None and state.stop_reason is not None:
                raise FreeSearchError(state.stop_reason) from None
            if error.category is ToolErrorCategory.RECONCILIATION_REQUIRED:
                raise FreeSearchError(FreeSearchStopReason.REQUEST_UNCERTAIN) from None
            raise

    def release(self, batch: SearchResultBatch) -> None:
        self._delegate.release(batch)

    def discard_all(self) -> None:
        self._delegate.discard_all()
