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
    query: InventoryQuery, strategy: str, supplement: str
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
        else ("Africa" if query.region == "africa" else "")
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
    return result
