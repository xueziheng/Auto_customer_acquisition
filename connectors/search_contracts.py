"""供应商无关的公开搜索结果、能力与用量契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import ValidationError

__all__ = (
    "SearchCapabilities",
    "SearchCostStatus",
    "SearchProvider",
    "SearchResult",
    "SearchUsage",
    "SearchUsageReader",
)


def _safe_text(value: object, maximum: int, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


@dataclass(frozen=True, repr=False)
class SearchResult:
    """搜索定位结果；不是页面证据，也不承载 provider 的相关性分数。"""

    title: str
    url: str
    description: str = field(repr=False)

    def __post_init__(self) -> None:
        if not (
            _safe_text(self.title, 500)
            and _safe_text(self.url, 2_048)
            and _safe_text(self.description, 2_000, allow_empty=True)
        ):
            raise ValidationError("公开搜索结果无效")


@dataclass(frozen=True)
class SearchCapabilities:
    """Provider 可验证的搜索能力，避免上层把偏好误作承诺。"""

    supports_country_boost: bool
    maximum_results: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.supports_country_boost, bool)
            or type(self.maximum_results) is not int
            or not 1 <= self.maximum_results <= 20
        ):
            raise ValidationError("公开搜索能力声明无效")


class SearchCostStatus(str, Enum):
    """账号计费状态；UNKNOWN 不代表套餐内额度已被证明免费。"""

    FREE = "free"
    PAID = "paid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SearchUsage:
    """Provider 公开返回的账户用量，不虚构周期或缺失字段。"""

    plan: str | None
    limit: int | None
    used: int | None
    paygo_enabled: bool | None
    cost_status: SearchCostStatus = SearchCostStatus.UNKNOWN
    included_credits_free: bool = False

    def __post_init__(self) -> None:
        if (
            (self.plan is not None and not _safe_text(self.plan, 120))
            or any(
                value is not None
                and (type(value) is not int or value < 0)
                for value in (self.limit, self.used)
            )
            or (
                self.paygo_enabled is not None
                and not isinstance(self.paygo_enabled, bool)
            )
            or not isinstance(self.cost_status, SearchCostStatus)
            or type(self.included_credits_free) is not bool
            or (
                self.included_credits_free
                and (
                    self.plan != "Researcher"
                    or self.limit is None
                    or self.used is None
                    or self.cost_status is SearchCostStatus.PAID
                    or self.paygo_enabled is True
                )
            )
        ):
            raise ValidationError("公开搜索用量无效")


@runtime_checkable
class SearchProvider(Protocol):
    """按原始查询返回网页定位结果；country 是偏好，不得改写查询文本。"""

    capabilities: SearchCapabilities

    async def search(
        self, query: str, *, country: str, limit: int
    ) -> tuple[SearchResult, ...]: ...


@runtime_checkable
class SearchUsageReader(Protocol):
    """读取 provider 账户的安全用量摘要，供额度门禁 fail-closed 使用。"""

    async def usage(self) -> SearchUsage: ...
