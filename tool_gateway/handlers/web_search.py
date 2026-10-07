"""`web.search` Gateway 插件与 task-local 搜索批次交接。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from connectors.web_search.client import (
    WebSearchConnector,
    WebSearchResult,
    WebSearchSecretResolver,
)
from connectors.web_search.transport import (
    PublicPageRejectedError,
    PublicPageRejectedReason,
    WebSearchAuthRequiredError,
    WebSearchProviderError,
    WebSearchRateLimitedError,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId
from tool_gateway.checks.web_discovery import WebResearchPreflight
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.web_slots import SearchResultBatch, WebSearchResultSlot
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import (
    PreparedToolCall,
    SafeScalar,
    ToolCallContext,
    ToolCallResult,
)

MANIFEST = ToolManifest(
    tool_id="web.search",
    version="v1",
    description="按已确认探索边界查询公开网页索引",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.MEDIUM,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("web:search",),
    checks=("tenant", "permission", "playbook", "country_policy", "rate_limit"),
    input_schema={
        "type": "object",
        "required": ("query", "country", "category", "limit"),
        "properties": {
            "query": {"type": "string", "maxLength": 400},
            "country": {"type": "string", "minLength": 2, "maxLength": 2},
            "category": {"type": "string", "maxLength": 100},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
    redact_fields=("query",),
)

_COUNTRY = re.compile(r"[A-Z]{2}")
_QUOTA_REQUEST_KEY = re.compile(r"[0-9a-f]{64}")


@runtime_checkable
class _ProviderWebSearcher(Protocol):
    async def search(
        self,
        tenant_id: TenantId,
        query: str,
        country: str,
        limit: int,
    ) -> tuple[WebSearchResult, ...]: ...


@runtime_checkable
class RunBoundWebSearcherFactory(Protocol):
    """prepare 阶段的无 IO 插件口；把显式 Run 和 HMAC 操作指纹绑定到 reader。"""

    def for_run(
        self, tenant_id: TenantId, run_id: RunId, request_key: str,
        *, fingerprint_version: str,
    ) -> _ProviderWebSearcher: ...


@runtime_checkable
class _ToolGatewayInvoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class ConnectorWebSearcher:
    """全部检查通过后才构造 connector 并解析 Brave 凭证。"""

    def __init__(
        self,
        connector_factory: Callable[[TenantId], WebSearchConnector],
        secret_resolver: WebSearchSecretResolver,
    ) -> None:
        if not callable(connector_factory) or not isinstance(
            secret_resolver, WebSearchSecretResolver
        ):
            raise ValidationError("公开搜索 reader 依赖无效")
        self._connector_factory = connector_factory
        self._secret_resolver = secret_resolver

    async def search(
        self,
        tenant_id: TenantId,
        query: str,
        country: str,
        limit: int,
    ) -> tuple[WebSearchResult, ...]:
        connector = self._connector_factory(tenant_id)
        if not isinstance(connector, WebSearchConnector):
            raise ValidationError("公开搜索 connector 无效")
        await connector.configure(self._secret_resolver)
        return await connector.search(query, country=country, limit=limit)


@dataclass(frozen=True, repr=False)
class _WebSearchPayload:
    tenant_id: TenantId
    query: str = field(repr=False)
    country: str
    category: str
    limit: int
    reader: _ProviderWebSearcher = field(repr=False)


class WebSearchHandler:
    """准备安全投影，执行后只返回 task-local `wsb_` handle。"""

    def __init__(
        self,
        reader: _ProviderWebSearcher | None,
        slot: WebSearchResultSlot,
        fingerprints: HmacFingerprintProvider,
        *,
        reader_factory: RunBoundWebSearcherFactory | None = None,
    ) -> None:
        if (
            (reader_factory is None and not isinstance(reader, _ProviderWebSearcher))
            or (
                reader_factory is not None
                and (
                    reader is not None
                    or not isinstance(reader_factory, RunBoundWebSearcherFactory)
                )
            )
            or not isinstance(slot, WebSearchResultSlot)
            or not isinstance(fingerprints, HmacFingerprintProvider)
        ):
            raise ValidationError("公开搜索 handler 依赖无效")
        self._reader = reader
        self._reader_factory = reader_factory
        self._slot = slot
        self._fingerprints = fingerprints

    async def prepare(
        self,
        ctx: ToolCallContext,
        preflight: object | None,
    ) -> PreparedToolCall:
        allowed = {"query", "country", "category", "limit"}
        if self._reader_factory is not None:
            allowed.add("quota_request_key")
        if not {"query", "country", "category", "limit"} <= set(
            ctx.params
        ) or not set(ctx.params) <= allowed:
            raise ValidationError("公开搜索工具参数无效")
        query = ctx.params.get("query")
        country = ctx.params.get("country")
        category = ctx.params.get("category")
        limit = ctx.params.get("limit")
        quota_request_key = ctx.params.get("quota_request_key")
        if (
            not _safe_text(query, 400)
            or not isinstance(query, str)
            or len(query.split()) > 50
            or not isinstance(country, str)
            or _COUNTRY.fullmatch(country) is None
            or not _safe_text(category, 100)
            or not isinstance(category, str)
            or type(limit) is not int
            or not 1 <= limit <= 20
            or (
                "quota_request_key" in ctx.params
                and (
                    not isinstance(quota_request_key, str)
                    or _QUOTA_REQUEST_KEY.fullmatch(quota_request_key) is None
                )
            )
            or not isinstance(preflight, WebResearchPreflight)
            or preflight.tenant_id != ctx.tenant_id
            or preflight.country != country
            or preflight.category != category
        ):
            raise ValidationError("公开搜索工具参数无效")
        fingerprint, version = self._fingerprints.fingerprint(
            (
                str(ctx.tenant_id).encode(),
                query.encode(),
                country.encode(),
                category.encode(),
                str(limit).encode(),
            )
        )
        reader = self._reader
        if self._reader_factory is not None:
            if ctx.run_id is None:
                raise ValidationError("公开搜索缺少 Run 绑定")
            reader = self._reader_factory.for_run(
                ctx.tenant_id,
                ctx.run_id,
                quota_request_key or fingerprint,
                fingerprint_version=version,
            )
        if not isinstance(reader, _ProviderWebSearcher):
            raise ValidationError("公开搜索 reader 绑定无效")
        return PreparedToolCall(
            fingerprint,
            version,
            {"country": country, "result_limit": limit},
            _WebSearchPayload(
                ctx.tenant_id,
                query,
                country,
                category,
                limit,
                reader,
            ),
        )

    async def execute(
        self,
        tenant_id: TenantId,
        prepared: PreparedToolCall,
    ) -> Mapping[str, SafeScalar]:
        payload = prepared.payload
        if not isinstance(payload, _WebSearchPayload) or payload.tenant_id != tenant_id:
            raise ValidationError("公开搜索 payload 无效")
        try:
            results = await payload.reader.search(
                tenant_id,
                payload.query,
                payload.country,
                payload.limit,
            )
        except (
            ToolGatewayError,
            WebSearchAuthRequiredError,
            WebSearchRateLimitedError,
            WebSearchProviderError,
            PublicPageRejectedError,
            TransientError,
            ValidationError,
        ) as error:
            raise map_web_provider_error(error) from None
        if (
            type(results) is not tuple
            or len(results) > payload.limit
            or any(not isinstance(item, WebSearchResult) for item in results)
        ):
            raise ValidationError("公开搜索 reader 结果无效")
        batch = self._slot.put(
            tenant_id,
            payload.country,
            payload.category,
            results,
        )
        return {"provider_ref": batch.handle}


class ToolGatewayWebSearcher:
    """workflow-facing adapter；结果只在本次 task 中可读并显式释放。"""

    def __init__(
        self,
        gateway: _ToolGatewayInvoker,
        slot: WebSearchResultSlot,
        user_id: UserId,
    ) -> None:
        if (
            not isinstance(gateway, _ToolGatewayInvoker)
            or not isinstance(slot, WebSearchResultSlot)
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ValidationError("公开搜索 adapter 依赖无效")
        self._gateway = gateway
        self._slot = slot
        self._user_id = user_id

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
        params: dict[str, object] = {
            "query": query,
            "country": country,
            "category": category,
            "limit": limit,
        }
        if quota_request_key is not None:
            params["quota_request_key"] = quota_request_key
        try:
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id,
                    self._user_id,
                    MANIFEST.tool_id,
                    params,
                    run_id=run_id,
                )
            )
            if not isinstance(result, ToolCallResult):
                raise ValidationError("公开搜索工具结果无效")
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ToolGatewayError(
                    result.error_category or ToolErrorCategory.UNEXPECTED,
                    retry_after_seconds=result.retry_after_seconds,
                )
            handle = (
                None if result.output is None else result.output.get("provider_ref")
            )
            if not isinstance(handle, str):
                raise ValidationError("公开搜索工具结果无效")
            batch = self._slot.get_batch(handle)
            if batch.tenant_id != tenant_id:
                raise ValidationError("公开搜索批次租户无效")
            return batch
        except BaseException:
            self._slot.discard_all()
            raise

    def release(self, batch: SearchResultBatch) -> None:
        if not isinstance(batch, SearchResultBatch):
            raise ValidationError("公开搜索批次无效")
        self._slot.discard(batch.handle)

    def discard_all(self) -> None:
        self._slot.discard_all()


def map_web_provider_error(error: BaseException) -> ToolGatewayError:
    if isinstance(error, ToolGatewayError):
        return error
    if isinstance(error, WebSearchAuthRequiredError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
    if isinstance(error, WebSearchRateLimitedError):
        return ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED,
            retry_after_seconds=error.retry_after_seconds,
        )
    if isinstance(error, TransientError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
    if isinstance(error, PublicPageRejectedError):
        return ToolGatewayError(
            {
                PublicPageRejectedReason.PAGE_ACCESS_FORBIDDEN: ToolErrorCategory.PAGE_ACCESS_FORBIDDEN,
                PublicPageRejectedReason.LOGIN_OR_CAPTCHA: ToolErrorCategory.LOGIN_OR_CAPTCHA,
                PublicPageRejectedReason.UNSAFE_REDIRECT: ToolErrorCategory.UNSAFE_REDIRECT,
            }[error.reason]
        )
    if isinstance(error, WebSearchProviderError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    if isinstance(error, ValidationError):
        return ToolGatewayError(ToolErrorCategory.VALIDATION)
    return ToolGatewayError(ToolErrorCategory.UNEXPECTED)


def _safe_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= maximum
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


__all__ = (
    "MANIFEST",
    "ConnectorWebSearcher",
    "RunBoundWebSearcherFactory",
    "ToolGatewayWebSearcher",
    "WebSearchHandler",
    "map_web_provider_error",
)
