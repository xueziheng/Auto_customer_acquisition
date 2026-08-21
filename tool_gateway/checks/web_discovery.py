"""公开搜索工具的租户、Playbook、国家政策与配额检查。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from shared.errors import PolicyViolation, ValidationError
from shared.schemas.identifiers import RunId, TenantId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolInvocationState,
)

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_RUN = re.compile(rf"run_{_ULID}")
_SEARCH_HANDLE = re.compile(rf"wsb_{_ULID}")
_COUNTRY = re.compile(r"[A-Z]{2}")


@dataclass(frozen=True)
class WebResearchPreflight:
    tenant_id: TenantId
    country: str
    category: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tenant_id, str)
            or not self.tenant_id
            or not isinstance(self.country, str)
            or _COUNTRY.fullmatch(self.country) is None
            or not _safe_category(self.category)
        ):
            raise ValidationError("公开搜索 preflight 无效")


@dataclass(frozen=True, repr=False)
class ApprovedSearchBatch:
    tenant_id: TenantId
    country: str
    category: str
    urls: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tenant_id, str)
            or not self.tenant_id
            or not isinstance(self.country, str)
            or _COUNTRY.fullmatch(self.country) is None
            or not _safe_category(self.category)
            or type(self.urls) is not tuple
            or len(self.urls) > 20
            or any(
                not isinstance(url, str)
                or not url
                or len(url) > 2_048
                or url != url.strip()
                for url in self.urls
            )
        ):
            raise ValidationError("批准搜索结果批次无效")


@runtime_checkable
class WebRunTenantReader(Protocol):
    async def owns_run(self, tenant_id: TenantId, run_id: RunId) -> bool: ...


@runtime_checkable
class WebResearchPlaybookReader(Protocol):
    async def allows_research(
        self,
        tenant_id: TenantId,
        category: str,
        country: str,
    ) -> bool: ...


@runtime_checkable
class WebResearchCountryPolicyReader(Protocol):
    async def allows_public_research(
        self,
        tenant_id: TenantId,
        country: str,
    ) -> bool: ...


@runtime_checkable
class ApprovedSearchBatchReader(Protocol):
    def get_approved_batch(self, handle: str) -> ApprovedSearchBatch: ...


@runtime_checkable
class WebProviderQuotaGuard(Protocol):
    async def reserve(
        self,
        tenant_id: TenantId,
        capability: str,
        now: datetime,
    ) -> int | None: ...


class WebResourceTenantCheck:
    name = "tenant"

    def __init__(self, reader: WebRunTenantReader) -> None:
        if not isinstance(reader, WebRunTenantReader):
            raise ValidationError("公开搜索租户 reader 无效")
        self._reader = reader

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        run_id = ctx.run_id
        if (
            not isinstance(run_id, str)
            or _RUN.fullmatch(run_id) is None
            or not await self._reader.owns_run(ctx.tenant_id, RunId(run_id))
        ):
            return CheckRejection(
                self.name,
                "tenant:workflow_run",
                "公开搜索工作流租户绑定无效",
            )
        return None


class WebResearchPlaybookCheck:
    name = "playbook"

    def __init__(
        self,
        reader: WebResearchPlaybookReader,
        batches: ApprovedSearchBatchReader,
    ) -> None:
        if not isinstance(reader, WebResearchPlaybookReader) or not isinstance(
            batches, ApprovedSearchBatchReader
        ):
            raise ValidationError("公开搜索 Playbook 依赖无效")
        self._reader = reader
        self._batches = batches

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        try:
            preflight = self._preflight(ctx)
            allowed = await self._reader.allows_research(
                ctx.tenant_id,
                preflight.category,
                preflight.country,
            )
        except (PolicyViolation, ValidationError):
            return CheckRejection(
                self.name,
                "playbook:research_not_allowed",
                "公司 Playbook 不允许本次公开搜索",
            )
        if not allowed:
            return CheckRejection(
                self.name,
                "playbook:research_not_allowed",
                "公司 Playbook 不允许本次公开搜索",
            )
        state.preflight = preflight
        return None

    def _preflight(self, ctx: ToolCallContext) -> WebResearchPreflight:
        if ctx.tool_id == "web.search":
            country = ctx.params.get("country")
            category = ctx.params.get("category")
            if not isinstance(country, str) or not isinstance(category, str):
                raise ValidationError("公开搜索 Playbook 参数无效")
            return WebResearchPreflight(ctx.tenant_id, country, category)
        if ctx.tool_id == "web.read_page":
            handle = ctx.params.get("search_result_handle")
            if (
                not isinstance(handle, str)
                or _SEARCH_HANDLE.fullmatch(handle) is None
            ):
                raise ValidationError("公开页面搜索批次无效")
            batch = self._batches.get_approved_batch(handle)
            if batch.tenant_id != ctx.tenant_id:
                raise ValidationError("公开页面搜索批次租户无效")
            return WebResearchPreflight(
                ctx.tenant_id,
                batch.country,
                batch.category,
            )
        raise ValidationError("公开搜索工具无效")


class WebResearchCountryPolicyCheck:
    name = "country_policy"

    def __init__(self, reader: WebResearchCountryPolicyReader) -> None:
        if not isinstance(reader, WebResearchCountryPolicyReader):
            raise ValidationError("公开搜索国家政策 reader 无效")
        self._reader = reader

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        preflight = state.preflight
        if (
            not isinstance(preflight, WebResearchPreflight)
            or preflight.tenant_id != ctx.tenant_id
        ):
            return CheckRejection(
                self.name,
                "country_policy:preflight_missing",
                "公开搜索国家政策事实缺失",
            )
        try:
            allowed = await self._reader.allows_public_research(
                ctx.tenant_id,
                preflight.country,
            )
        except (PolicyViolation, ValidationError):
            allowed = False
        if not allowed:
            return CheckRejection(
                self.name,
                "country_policy:research_not_allowed",
                "目标国家政策不允许本次公开搜索",
            )
        return None


class WebProviderRateLimitCheck:
    name = "rate_limit"

    def __init__(
        self,
        quota: WebProviderQuotaGuard,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(quota, WebProviderQuotaGuard) or not callable(now):
            raise ValidationError("公开搜索配额依赖无效")
        self._quota = quota
        self._now = now

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is not UTC:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        retry_after = await self._quota.reserve(ctx.tenant_id, ctx.tool_id, now)
        if retry_after is None:
            return None
        if type(retry_after) is not int or not 1 <= retry_after <= 86_400:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        raise ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED,
            retry_after_seconds=retry_after,
        )


def _safe_category(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 100
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


__all__ = (
    "ApprovedSearchBatch",
    "ApprovedSearchBatchReader",
    "WebProviderQuotaGuard",
    "WebProviderRateLimitCheck",
    "WebResearchCountryPolicyCheck",
    "WebResearchCountryPolicyReader",
    "WebResearchPlaybookCheck",
    "WebResearchPlaybookReader",
    "WebResearchPreflight",
    "WebResourceTenantCheck",
    "WebRunTenantReader",
)
