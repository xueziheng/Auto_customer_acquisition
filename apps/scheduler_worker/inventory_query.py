"""库存查询的有界文本组装；检索锚点不是国家、官网或产品事实证据。"""

from __future__ import annotations

import unicodedata

from domains.prospecting.inventory import AFRICA_COUNTRIES, InventoryQuery
from domains.prospecting.public_evidence import COUNTRY_NAMES, TAG_ALIASES


def _plain_spacing(value: str) -> str:
    if not isinstance(value, str) or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("采集检索条件无效")
    return " ".join(value.split())


def compose_inventory_query(
    query: InventoryQuery,
    strategy: str,
    supplement: str,
    *,
    public_domains: tuple[str, ...] = (),
) -> str:
    """完整保留任务首产品/地域/受信策略，仅补充普通词；不扩大查询或证据范围。"""
    if (
        query.region == "africa"
        and query.country
        and query.country not in AFRICA_COUNTRIES
    ):
        raise ValueError("采集检索条件无效")
    # 首项沿既有category；标签已规范排序，不代表用户优先级或所有标签召回。
    tag = query.product_tags[0]
    product = TAG_ALIASES.get(tag, (tag,))[0]
    place = (
        COUNTRY_NAMES.get(query.country, query.country)
        if query.country
        else (query.region.title() if query.region else "")
    )
    if query.regions or query.countries:
        place = (
            " ".join(COUNTRY_NAMES.get(code, code) for code in query.target_countries)
            if query.countries
            else " ".join(
                sorted(set(query.regions) | ({query.region} if query.region else set()))
            )
        )
    fixed = " ".join(
        part
        for part in (
            _plain_spacing(product),
            _plain_spacing(place),
            _plain_spacing(strategy),
        )
        if part
    )
    if not fixed or len(fixed) > 400 or len(fixed.split()) > 50:
        raise ValueError("采集检索条件无效")
    text = _plain_spacing(supplement)
    if query.confirmed_refs or query.intent:
        # 私有上下文进入模型后，补充词只能选用户公开锚点、固定策略或本轮已观察域名。
        allowed = {
            word.casefold()
            for word in (" ".join(query.product_tags) + " " + strategy).split()
        }
        text = " ".join(
            word
            for word in text.split()
            if word.casefold() in allowed or word.lower() in public_domains
        )
        # 保留完整公开域名供官网跟进，拒绝任意字符串拼接外泄。
        domains = [
            word.lower() for word in text.split() if word.lower() in public_domains
        ]
        text = " ".join(
            word for word in text.split() if word.lower() not in public_domains
        )
    else:
        domains = []
    words = "".join(
        character if character.isalnum() else " " for character in text
    ).split()
    result = fixed
    seen = {word.casefold() for word in fixed.split()}
    count = len(fixed.split())
    for word in words:
        normalized = word.casefold()
        if normalized in {"and", "or", "not"} or normalized in seen:
            continue
        if count >= 50:
            break
        if len(result) + 1 + len(word) > 400:
            continue
        result += " " + word
        seen.add(normalized)
        count += 1
    for domain in dict.fromkeys(domains):
        if count < 50 and len(result) + len(domain) + 1 <= 400:
            result += " " + domain
            count += 1
    return result
