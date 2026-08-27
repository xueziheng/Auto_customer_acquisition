"""Tavily 搜索 connector：只返回网页定位结果与保守用量摘要。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeGuard, runtime_checkable

from connectors.search_contracts import (
    SearchCapabilities,
    SearchCostStatus,
    SearchResult,
    SearchUsage,
)
from connectors.web_search.transport import PublicPageTransport
from shared.errors import ValidationError

from .manifest import MANIFEST
from .transport import (
    TavilyAuthRequiredError,
    TavilyHttpResponse,
    TavilySearchTransport,
)

TAVILY_API_KEY_REF = "TAVILY_API_KEY_REF"
_COUNTRY = re.compile(r"[A-Z]{2}")


@runtime_checkable
class TavilySecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


@dataclass(frozen=True, repr=False)
class _SecretValue:
    value: str = field(repr=False)


class TavilySearchConnector:
    """Tavily 的最小免费搜索能力；页面证据仍由现有安全读取器生成。"""

    manifest = MANIFEST
    capabilities = SearchCapabilities(supports_country_boost=False, maximum_results=20)

    def __init__(
        self,
        transport: TavilySearchTransport,
        page_transport: PublicPageTransport,
    ) -> None:
        if (
            not isinstance(transport, TavilySearchTransport)
            or not isinstance(page_transport, PublicPageTransport)
        ):
            raise ValidationError("Tavily 连接器依赖无效")
        self._transport = transport
        self._page_transport = page_transport
        self._api_key: _SecretValue | None = None

    def __repr__(self) -> str:
        return "TavilySearchConnector()"

    async def configure(self, secret_resolver: TavilySecretResolver) -> None:
        """惰性解析部署凭证；任何 resolver 异常均在 connector 边界脱敏。"""
        if not isinstance(secret_resolver, TavilySecretResolver):
            raise ValidationError("Tavily 凭证解析器无效")
        resolution_failed = False
        api_key: object = None
        try:
            api_key = secret_resolver.resolve(TAVILY_API_KEY_REF)
        except Exception:  # noqa: BLE001 - resolver 是凭证边界，异常不得穿透。
            resolution_failed = True
        if resolution_failed or not _valid_api_key(api_key):
            raise TavilyAuthRequiredError()
        self._api_key = _SecretValue(api_key)

    async def health_check(self) -> bool:
        """仅表示凭证已被 connector 接受；不为检查而产生外部收费调用。"""
        return self._api_key is not None

    async def search(
        self, query: str, *, country: str, limit: int
    ) -> tuple[SearchResult, ...]:
        """执行固定 basic 搜索，丢弃 provider 分数并复用 URL 安全校验。"""
        _validate_search_input(query, country, limit)
        api_key = self._configured_key()
        response = await self._transport.search(query, country, limit, api_key=api_key)
        _require_ok(response, "Tavily 搜索响应无效")
        raw_results = response.payload.get("results")
        if not isinstance(raw_results, list):
            raise ValidationError("Tavily 搜索响应无效")
        results: list[SearchResult] = []
        for item in raw_results[:limit]:
            if not isinstance(item, Mapping):
                raise ValidationError("Tavily 搜索响应无效")
            title = item.get("title")
            url = item.get("url")
            description = item.get("content")
            if (
                not isinstance(title, str)
                or not isinstance(url, str)
                or not isinstance(description, str)
            ):
                raise ValidationError("Tavily 搜索响应无效")
            canonical_url = await self._page_transport.validate_url(url)
            results.append(SearchResult(title, canonical_url, description))
        return tuple(results)

    async def usage(self) -> SearchUsage:
        """严格解码官方 `/usage`；字段缺失保留未知，绝不回填零或周期。"""
        response = await self._transport.usage(api_key=self._configured_key())
        _require_ok(response, "Tavily 用量响应无效")
        return _decode_usage(response.payload)

    def _configured_key(self) -> str:
        if self._api_key is None:
            raise TavilyAuthRequiredError()
        return self._api_key.value


def _decode_usage(payload: Mapping[str, object]) -> SearchUsage:
    account = payload.get("account")
    if account is None:
        return SearchUsage(None, None, None, None)
    if not isinstance(account, Mapping):
        raise ValidationError("Tavily 用量响应无效")
    plan = _optional_text(account, "current_plan")
    used = _optional_count(account, "plan_usage")
    limit = _optional_count(account, "plan_limit")
    paygo_usage = _optional_count(account, "paygo_usage")
    paygo_limit = _optional_count(account, "paygo_limit")
    paygo_enabled: bool | None
    if paygo_usage is None or paygo_limit is None:
        paygo_enabled = None
    elif paygo_usage == 0 and paygo_limit == 0:
        paygo_enabled = False
    else:
        paygo_enabled = True
    return SearchUsage(
        plan,
        limit,
        used,
        paygo_enabled,
        _classify_cost_status(plan, paygo_enabled),
    )


def _classify_cost_status(
    plan: str | None, paygo_enabled: bool | None
) -> SearchCostStatus:
    """只接受官方精确套餐名；其他值不猜测为免费或付费。"""
    if paygo_enabled is True or plan in {"Project", "Bootstrap", "Startup", "Growth"}:
        return SearchCostStatus.PAID
    if plan == "Researcher" and paygo_enabled is False:
        return SearchCostStatus.FREE
    return SearchCostStatus.UNKNOWN


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError("Tavily 用量响应无效")
    return value


def _optional_count(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValidationError("Tavily 用量响应无效")
    return value


def _require_ok(response: TavilyHttpResponse, message: str) -> None:
    if response.status_code != 200:
        raise ValidationError(message)


def _valid_api_key(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) >= 16
        and value == value.strip()
        and not any(ord(character) < 33 or ord(character) == 127 for character in value)
    )


def _validate_search_input(query: str, country: str, limit: int) -> None:
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= 400
        or len(query.split()) > 50
        or query != query.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in query)
        or not isinstance(country, str)
        or _COUNTRY.fullmatch(country) is None
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise ValidationError("Tavily 搜索参数无效")


__all__ = (
    "TAVILY_API_KEY_REF",
    "TavilySearchConnector",
    "TavilySecretResolver",
)
