"""研究预算与范围从明确字段收集；模糊自然语言先请员工澄清，不猜预算。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import ValidationError as SchemaError

from agent_runtime.assistant.context import AssistantContext
from domains.assistant.schemas import Clarification, ResearchDraft, SourcedField
from domains.directives.schemas import (
    DemandDiscoveryPlanInput,
    DiscoverySearchQueryInput,
)
from shared.errors import PermissionDenied
from shared.schemas.evidence import ConfidenceTier

LABELS: dict[str, tuple[str, ...]] = {
    "objective": ("研究目标", "目标"),
    "target_countries": ("目标国家", "国家", "市场"),
    "target_categories": ("目标品类", "品类"),
    "excluded_countries": ("排除国家",),
    "excluded_categories": ("排除品类",),
    "max_search_queries": ("搜索次数", "查询预算"),
    "max_pages_read": ("页面数", "页面预算"),
    "max_signals": ("信号数",),
    "max_hypotheses": ("假设数",),
    "minimum_confidence_tier": ("证据门槛",),
    "strategy_group": ("策略组",),
    "query_limit": ("每次搜索结果数",),
}
_TIERS = {"low", "low_mid", "mid", "mid_high", "high", "very_high", "extreme"}


def unsupported_fields(
    fields: Sequence[SourcedField], allowed: Mapping[str, frozenset[str]]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {f.name for f in fields if f.value not in allowed.get(f.name, frozenset())}
        )
    )


def collect_fields(context: AssistantContext) -> dict[str, SourcedField]:
    """字段标签限定值语义；后续明确修改覆盖早先原话，含糊冲突不推断。"""
    values: dict[str, SourcedField] = {}
    for turn in context.turns:
        if turn.content_hidden:
            continue
        for name, labels in LABELS.items():
            pattern = (
                r"(?:^|[；;\n])\s*(?:"
                + "|".join(re.escape(label) for label in (name, *labels))
                + r")\s*[:：=]\s*([^；;\n]+)"
            )
            found = re.findall(pattern, turn.input_text)
            if found:
                if len(set(found)) != 1:
                    values.pop(name, None)
                else:
                    try:
                        values[name] = SourcedField(
                            name=name, value=found[-1].strip(), source_turn_id=turn.turn_id
                        )
                    except SchemaError:
                        # 无效新值必须清除旧值，不能回退旧预算或阻断之后的纠正。
                        values.pop(name, None)
    return values


def missing(fields: Sequence[str]) -> Clarification:
    names = tuple(sorted(set(fields)))
    labels = "；".join(f"{LABELS[n][0]}=明确值" if n in LABELS else n for n in names)
    return Clarification(
        questions=(
            f"请明确这些研究条件：{labels}。国家用两位代码，多个值用逗号；没有排除项请写“无”。这只会准备待确认提案。",
        ),
        missing_fields=names,
    )


def _items(value: str) -> tuple[str, ...]:
    if value == "无":
        return ()
    return tuple(v.strip() for v in re.split(r"[,，]", value))


class ResearchProposalBuilder:
    def __init__(self, policy_fields: Sequence[SourcedField] = ()) -> None:
        # 装配者只可传当前已确认的政策；旧政策不可作为默认值留在全局缓存。
        self._policy_fields = tuple(policy_fields)

    async def build(
        self, context: AssistantContext, draft: ResearchDraft
    ) -> DemandDiscoveryPlanInput | Clarification:
        if context.role != "boss":
            raise PermissionDenied("研究提案需由老板准备并另行确认")
        candidates = {
            f.name: f for f in self._policy_fields if f.policy_version is not None
        }
        candidates.update(collect_fields(context))
        supplied = {f.name: f for f in draft.fields}
        bad = [
            name
            for name in LABELS
            if name not in supplied
            or name not in candidates
            or supplied[name] != candidates[name]
        ]
        bad.extend(f.name for f in draft.fields if f.name not in LABELS)
        if len(supplied) != len(draft.fields):
            bad.extend(f.name for f in draft.fields)
        if bad:
            return missing(bad)
        values = {name: f.value for name, f in supplied.items()}
        budgets: dict[str, int] = {}
        for name, maximum in (
            ("max_search_queries", 100),
            ("max_pages_read", 50),
            ("max_signals", 100),
            ("max_hypotheses", 100),
            ("query_limit", 20),
        ):
            if (
                not re.fullmatch(r"[1-9][0-9]*", values[name])
                or not 1 <= int(values[name]) <= maximum
            ):
                return missing([name])
            budgets[name] = int(values[name])
        countries, categories = (
            _items(values["target_countries"]),
            _items(values["target_categories"]),
        )
        excluded_countries, excluded_categories = (
            _items(values["excluded_countries"]),
            _items(values["excluded_categories"]),
        )
        if (
            not countries
            or any(
                not re.fullmatch(r"[A-Z]{2}", c)
                for c in (*countries, *excluded_countries)
            )
            or set(countries) & set(excluded_countries)
        ):
            return missing(["target_countries", "excluded_countries"])
        if (
            not categories
            or any(not c or len(c) > 100 for c in (*categories, *excluded_categories))
            or set(categories) & set(excluded_categories)
        ):
            return missing(["target_categories", "excluded_categories"])
        if values["minimum_confidence_tier"] not in _TIERS:
            return missing(["minimum_confidence_tier"])
        if (
            not values["strategy_group"]
            or len(values["strategy_group"]) > 64
            or len(values["objective"]) > 1000
        ):
            return missing(["strategy_group", "objective"])
        if len(set(countries)) != len(countries) or len(set(categories)) != len(
            categories
        ):
            return missing(["target_countries", "target_categories"])
        if len(countries) * len(categories) * 3 > budgets["max_search_queries"]:
            return missing(
                ["max_search_queries", "target_countries", "target_categories"]
            )
        queries = tuple(
            DiscoverySearchQueryInput(
                query=f"{country} {category} {lane}",
                country=country,
                category=category,
                limit=budgets["query_limit"],
                discovery_lane=lane,
            )
            for country in countries
            for category in categories
            for lane in ("importer", "distributor", "ecommerce")
        )
        return DemandDiscoveryPlanInput(
            objective=values["objective"],
            queries=queries,
            target_countries=countries,
            target_categories=categories,
            excluded_countries=excluded_countries,
            excluded_categories=excluded_categories,
            max_search_queries=budgets["max_search_queries"],
            max_pages_read=budgets["max_pages_read"],
            max_signals=budgets["max_signals"],
            max_hypotheses=budgets["max_hypotheses"],
            minimum_confidence_tier=cast(
                ConfidenceTier, values["minimum_confidence_tier"]
            ),
            strategy_group=values["strategy_group"],
            campaign_id="",
            role_hints=(),
            assessment_ref="",
            execution_mode="research_only",
        )
