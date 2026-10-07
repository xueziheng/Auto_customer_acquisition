"""在已确认预算内分配独立公开来源；查询方向不等于页面事实或采购需求。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from domains.directives.schemas import DiscoverySearchQueryInput
from shared.errors import ValidationError

_BASE_LANES = ("importer", "distributor", "ecommerce")
_TED_MARKETS = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
        "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
        "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO",
    }
)


@dataclass(frozen=True)
class _SourceTemplate:
    channel: str
    lane: str
    suffix: str
    countries: frozenset[str] | None = None


_SOURCES = (
    _SourceTemplate("industry_directory", "distributor", "industry directory companies"),
    _SourceTemplate("association_members", "importer", "trade association member directory"),
    _SourceTemplate("trade_show_exhibitors", "distributor", "trade show exhibitor list"),
    _SourceTemplate("public_procurement", "importer", "procurement tender request for quotation"),
    _SourceTemplate("company_news", "importer", "company expansion new facility procurement hiring"),
    _SourceTemplate("public_linkedin_company", "distributor", "site:linkedin.com/company/"),
    _SourceTemplate("public_trade_records", "importer", "public shipment records bill of lading importer"),
    _SourceTemplate("public_procurement", "importer", "site:ted.europa.eu/en/notice/", _TED_MARKETS),
    _SourceTemplate("public_procurement", "importer", "site:find-tender.service.gov.uk/Notice/", frozenset({"GB"})),
)


def _bounded_query(country: str, category: str, suffix: str) -> str:
    query = f"{country} {category} {suffix}"
    if (
        len(query) > 400
        or len(query.split()) > 50
        or any(ord(character) < 32 or ord(character) == 127 for character in query)
    ):
        raise ValidationError("研究品类无法构成有界查询")
    return query


def build_discovery_queries(
    *,
    countries: tuple[str, ...],
    categories: tuple[str, ...],
    max_queries: int,
    result_limit: int,
) -> tuple[DiscoverySearchQueryInput, ...]:
    """保留基础三线路，再按来源轮流覆盖市场与品类；不为用完预算重复查询。

    预算与范围必须已由提案构建器核对原话。这里再次限制查询形状，确保扩展
    模板不会突破工具输入上限；官方采购网址只是搜索限定，不授权调用其 API。
    """
    if (
        type(max_queries) is not int
        or not 1 <= max_queries <= 100
        or type(result_limit) is not int
        or not 1 <= result_limit <= 20
        or not countries
        or not categories
        or len(countries) != len(set(countries))
        or len(categories) != len(set(categories))
        or len(countries) * len(categories) * len(_BASE_LANES) > max_queries
        or any(re.fullmatch(r"[A-Z]{2}", country) is None for country in countries)
        or any(not category or category != category.strip() or len(category) > 100 for category in categories)
    ):
        raise ValidationError("研究查询范围或预算无效")

    queries: list[DiscoverySearchQueryInput] = []
    seen: set[str] = set()

    def append(country: str, category: str, lane: str, suffix: str) -> None:
        query = _bounded_query(country, category, suffix)
        if query not in seen:
            seen.add(query)
            queries.append(DiscoverySearchQueryInput(
                query=query, country=country, category=category,
                limit=result_limit, discovery_lane=lane,
            ))

    for country in countries:
        for category in categories:
            for lane in _BASE_LANES:
                append(country, category, lane, lane)

    for source in _SOURCES:
        for country in countries:
            if source.countries is not None and country not in source.countries:
                continue
            for category in categories:
                if len(queries) >= max_queries:
                    return tuple(queries)
                append(country, category, source.lane, source.suffix)
    return tuple(queries)


def query_source_channel(query: str) -> str:
    """识别本模块的固定检索模板；不据此认定实际网页类型或数据授权。

    历史、模型自行生成或无法识别的查询仍是一般公开搜索，不能推断它访问过
    特定来源。完整查询继续保存在提案与证据里，可逐条审计。
    """
    if not isinstance(query, str):
        return "public_web"
    for source in _SOURCES:
        marker = " " + source.suffix
        if query.endswith(marker):
            scope = query.removesuffix(marker)
            country, separator, category = scope.partition(" ")
            if (
                separator
                and re.fullmatch(r"[A-Z]{2}", country) is not None
                and category
                and category == category.strip()
                and len(category) <= 100
                and len(query) <= 400
                and len(query.split()) <= 50
                and not any(ord(character) < 32 or ord(character) == 127 for character in query)
                and (source.countries is None or country in source.countries)
            ):
                return source.channel
    return "public_web"
