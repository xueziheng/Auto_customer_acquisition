"""`web.read_page` Gateway 插件与一次性页面快照交接。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from connectors.web_search.client import PageSnapshot, WebSearchConnector
from connectors.web_search.transport import (
    PublicPageRejectedError,
    WebSearchAuthRequiredError,
    WebSearchProviderError,
    WebSearchRateLimitedError,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId
from tool_gateway.checks.web_discovery import WebResearchPreflight
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.web_search import map_web_provider_error
from tool_gateway.handlers.web_slots import (
    SearchResultBatch,
    WebPageSnapshotSlot,
    WebSearchResultSlot,
)
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
    tool_id="web.read_page",
    version="v1",
    description="读取已批准搜索批次中的公开 HTML 页面并生成不可变快照",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.LOW,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("web:read_page",),
    checks=("tenant", "permission", "playbook", "country_policy", "rate_limit"),
    input_schema={
        "type": "object",
        "required": ("search_result_handle", "result_index"),
        "properties": {
            "search_result_handle": {"type": "string"},
            "result_index": {"type": "integer", "minimum": 0, "maximum": 19},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
    redact_fields=(),
)

_SEARCH_HANDLE = re.compile(r"wsb_[0-7][0-9A-HJKMNP-TV-Z]{25}")


@runtime_checkable
class _ProviderPageReader(Protocol):
    async def read_page(
        self,
        tenant_id: TenantId,
        url: str,
        uploaded_by: UserId,
    ) -> PageSnapshot: ...


@runtime_checkable
class _ToolGatewayInvoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class ConnectorPublicPageReader:
    """全部检查通过后才创建 connector；页面读取本身不解析搜索凭证。"""

    def __init__(
        self,
        connector_factory: Callable[[TenantId], WebSearchConnector],
    ) -> None:
        if not callable(connector_factory):
            raise ValidationError("公开页面 reader 依赖无效")
        self._connector_factory = connector_factory

    async def read_page(
        self,
        tenant_id: TenantId,
        url: str,
        uploaded_by: UserId,
    ) -> PageSnapshot:
        connector = self._connector_factory(tenant_id)
        if not isinstance(connector, WebSearchConnector):
            raise ValidationError("公开页面 connector 无效")
        return await connector.read_page(
            tenant_id,
            url,
            uploaded_by=uploaded_by,
        )


@dataclass(frozen=True, repr=False)
class _WebPagePayload:
    tenant_id: TenantId
    url: str = field(repr=False)
    uploaded_by: UserId


class WebReadPageHandler:
    """URL 只从 task-local 搜索批次读取，ledger 仅记录句柄与序号。"""

    def __init__(
        self,
        reader: _ProviderPageReader,
        search_slot: WebSearchResultSlot,
        page_slot: WebPageSnapshotSlot,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        if (
            not isinstance(reader, _ProviderPageReader)
            or not isinstance(search_slot, WebSearchResultSlot)
            or not isinstance(page_slot, WebPageSnapshotSlot)
            or not isinstance(fingerprints, HmacFingerprintProvider)
        ):
            raise ValidationError("公开页面 handler 依赖无效")
        self._reader = reader
        self._search_slot = search_slot
        self._page_slot = page_slot
        self._fingerprints = fingerprints

    async def prepare(
        self,
        ctx: ToolCallContext,
        preflight: object | None,
    ) -> PreparedToolCall:
        if set(ctx.params) != {"search_result_handle", "result_index"}:
            raise ValidationError("公开页面工具参数无效")
        handle = ctx.params.get("search_result_handle")
        index = ctx.params.get("result_index")
        if (
            not isinstance(handle, str)
            or _SEARCH_HANDLE.fullmatch(handle) is None
            or type(index) is not int
            or not 0 <= index <= 19
            or not isinstance(preflight, WebResearchPreflight)
            or preflight.tenant_id != ctx.tenant_id
        ):
            raise ValidationError("公开页面工具参数无效")
        batch = self._search_slot.get_batch(handle)
        if (
            batch.tenant_id != ctx.tenant_id
            or batch.country != preflight.country
            or batch.category != preflight.category
            or index >= len(batch.results)
        ):
            raise ValidationError("公开页面搜索结果绑定无效")
        url = batch.results[index].url
        fingerprint, version = self._fingerprints.fingerprint(
            (
                str(ctx.tenant_id).encode(),
                handle.encode(),
                str(index).encode(),
                url.encode(),
            )
        )
        return PreparedToolCall(
            fingerprint,
            version,
            {"search_batch_id": handle, "result_index": index},
            _WebPagePayload(ctx.tenant_id, url, ctx.user_id),
        )

    async def execute(
        self,
        tenant_id: TenantId,
        prepared: PreparedToolCall,
    ) -> Mapping[str, SafeScalar]:
        payload = prepared.payload
        if not isinstance(payload, _WebPagePayload) or payload.tenant_id != tenant_id:
            raise ValidationError("公开页面 payload 无效")
        try:
            snapshot = await self._reader.read_page(
                tenant_id,
                payload.url,
                payload.uploaded_by,
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
            self._page_slot.discard_all()
            raise map_web_provider_error(error) from None
        if not isinstance(snapshot, PageSnapshot):
            self._page_slot.discard_all()
            raise ValidationError("公开页面 reader 结果无效")
        try:
            handle = self._page_slot.put(snapshot)
        except BaseException:
            self._page_slot.discard_all()
            raise
        return {"provider_ref": handle}


class ToolGatewayWebPageReader:
    """workflow-facing adapter；调用方不能提交任意 URL。"""

    def __init__(
        self,
        gateway: _ToolGatewayInvoker,
        page_slot: WebPageSnapshotSlot,
        user_id: UserId,
    ) -> None:
        if (
            not isinstance(gateway, _ToolGatewayInvoker)
            or not isinstance(page_slot, WebPageSnapshotSlot)
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ValidationError("公开页面 adapter 依赖无效")
        self._gateway = gateway
        self._page_slot = page_slot
        self._user_id = user_id

    async def read_page(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        batch: SearchResultBatch,
        result_index: int,
    ) -> PageSnapshot:
        if not isinstance(batch, SearchResultBatch) or batch.tenant_id != tenant_id:
            raise ValidationError("公开页面搜索批次无效")
        try:
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id,
                    self._user_id,
                    MANIFEST.tool_id,
                    {
                        "search_result_handle": batch.handle,
                        "result_index": result_index,
                    },
                    run_id=run_id,
                )
            )
            if not isinstance(result, ToolCallResult):
                raise ValidationError("公开页面工具结果无效")
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ToolGatewayError(
                    result.error_category or ToolErrorCategory.UNEXPECTED,
                    retry_after_seconds=result.retry_after_seconds,
                )
            handle = None if result.output is None else result.output.get("provider_ref")
            if not isinstance(handle, str):
                raise ValidationError("公开页面工具结果无效")
            snapshot = self._page_slot.take(handle)
            if not isinstance(snapshot, PageSnapshot):
                raise ValidationError("公开页面快照槽结果无效")
            return snapshot
        except BaseException:
            self._page_slot.discard_all()
            raise


__all__ = (
    "MANIFEST",
    "ConnectorPublicPageReader",
    "ToolGatewayWebPageReader",
    "WebReadPageHandler",
)
