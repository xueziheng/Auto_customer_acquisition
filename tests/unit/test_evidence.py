"""shared.schemas.evidence 的契约单测（行为断言，无 Any/type-ignore/noqa）。

覆盖（拦截的变异）：
1. 7 个 EvidenceLevel → 7 个 ConfidenceTier 完整参数映射（映射错位）
2. 独立上浮只看最高等级层：2 条不同 source_id → +1；同 source_id 不浮；
   3 条独立仍只 +1；1 条高等级 + 2 条低等级独立不浮（独立性错/累加/低等级抬升高等级）
3. 矛盾下浮一档 + has_conflict=True；conflicting_pairs 顺序不敏感；未知 source_id 忽略
4. 严格早于边界才 stale；恰好边界新鲜；至少一条新鲜不 stale
5. EXTREME 上浮与 LOW 受冲突+过期均被钳制（越界）
6. 空证据与 freshness_window 0/负数抛 shared.errors.ValidationError（无证据当 LOW/非正窗口放行）
7. explanation 非空且提及基准等级；applied_rules 用固定规则名
8. meets_threshold 覆盖大于 / 等于 / 小于
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.errors import ValidationError
from shared.schemas.evidence import (
    ConfidenceResult,
    ConfidenceTier,
    EvidenceItem,
    EvidenceLevel,
    derive_confidence,
    meets_threshold,
)

_NOW = datetime(2026, 8, 8, tzinfo=UTC)
_WINDOW = timedelta(days=7)

_FIXED_RULE_NAMES = {
    "base_from_highest",
    "independence_boost",
    "conflict_penalty",
    "staleness_penalty",
    "clamp",
}


def _item(
    level: EvidenceLevel,
    source_id: str,
    observed_at: datetime | None = None,
    summary: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        level=level,
        source_type="conversation",
        source_id=source_id,
        observed_at=observed_at if observed_at is not None else _NOW,
        summary=summary if summary is not None else "evidence summary",
    )


# --- 1. 等级 → 基准档完整映射 -------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (EvidenceLevel.AGENT_INDUSTRY_INFERENCE, ConfidenceTier.LOW),
        (EvidenceLevel.PUBLIC_COMPANY_EVENT, ConfidenceTier.LOW_MID),
        (EvidenceLevel.EMPLOYEE_GUESS, ConfidenceTier.MID),
        (EvidenceLevel.CUSTOMER_INTEREST_REPLY, ConfidenceTier.MID_HIGH),
        (EvidenceLevel.CUSTOMER_SPECIFICATION, ConfidenceTier.HIGH),
        (EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING, ConfidenceTier.VERY_HIGH),
        (EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST, ConfidenceTier.EXTREME),
    ],
)
def test_level_to_base_tier_mapping(level: EvidenceLevel, expected: ConfidenceTier) -> None:
    r = derive_confidence([_item(level, "s1")], now=_NOW)
    assert r.tier == expected


# --- 2. 独立上浮只看最高等级层 -------------------------------------------------


def test_independence_two_sources_boost_one_tier() -> None:
    r = derive_confidence(
        [_item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a"), _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "b")],
        now=_NOW,
    )
    assert r.tier == ConfidenceTier.MID  # LOW_MID +1
    assert "independence_boost" in r.applied_rules


def test_independence_same_source_no_boost() -> None:
    r = derive_confidence(
        [_item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a"), _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a")],
        now=_NOW,
    )
    assert r.tier == ConfidenceTier.LOW_MID
    assert "independence_boost" not in r.applied_rules


def test_independence_three_sources_still_one_tier() -> None:
    r = derive_confidence(
        [
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a"),
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "b"),
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "c"),
        ],
        now=_NOW,
    )
    assert r.tier == ConfidenceTier.MID  # 仍只 +1
    assert "independence_boost" in r.applied_rules


def test_independence_lower_tier_does_not_boost_highest() -> None:
    r = derive_confidence(
        [
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "high"),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "low1"),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "low2"),
        ],
        now=_NOW,
    )
    assert r.tier == ConfidenceTier.LOW_MID  # 上浮只看最高等级层
    assert "independence_boost" not in r.applied_rules


# --- 3. 矛盾：下浮 + has_conflict；顺序不敏感；未知 source_id 忽略 ------------


def test_conflict_floats_down_and_sets_flag() -> None:
    r = derive_confidence(
        [_item(EvidenceLevel.EMPLOYEE_GUESS, "b"), _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a")],
        now=_NOW,
        conflicting_pairs=[("a", "b")],
    )
    assert r.tier == ConfidenceTier.LOW_MID  # 基准 MID -1
    assert r.has_conflict is True
    assert "conflict_penalty" in r.applied_rules


def test_conflict_pairs_order_insensitive() -> None:
    items = [
        _item(EvidenceLevel.EMPLOYEE_GUESS, "b"),
        _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a"),
    ]
    forward = derive_confidence(items, now=_NOW, conflicting_pairs=[("a", "b")])
    reversed_pair = derive_confidence(items, now=_NOW, conflicting_pairs=[("b", "a")])
    assert forward.tier == reversed_pair.tier
    assert forward.has_conflict is True
    assert reversed_pair.has_conflict is True


@pytest.mark.parametrize(
    "pairs",
    [
        [("a", "zzz")],  # 一端已知、一端未知
        [("zzz", "a")],  # 反向
        [("zzz", "yyy")],  # 两端都未知
    ],
)
def test_conflict_unknown_source_id_ignored(pairs: list[tuple[str, str]]) -> None:
    r = derive_confidence(
        [_item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a")],
        now=_NOW,
        conflicting_pairs=pairs,
    )
    assert r.has_conflict is False
    assert r.tier == ConfidenceTier.LOW_MID


def test_multiple_conflict_pairs_only_one_tier_down() -> None:
    # 多条有效矛盾对只下浮一档（规则是"存在冲突"，不是每对扣一档）。
    # 最高等级层只有 a 一条（EMPLOYEE_GUESS），无独立 boost 干扰。
    r = derive_confidence(
        [
            _item(EvidenceLevel.EMPLOYEE_GUESS, "a"),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "b"),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "c"),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "d"),
        ],
        now=_NOW,
        conflicting_pairs=[("a", "b"), ("a", "c"), ("a", "d")],
    )
    assert r.tier == ConfidenceTier.LOW_MID  # 基准 MID，冲突只扣一档
    assert r.has_conflict is True


def test_conflict_after_boost_keeps_extreme_with_clamp() -> None:
    # 两条独立 EXTREME 且相互冲突：正确顺序 = 先 boost 到越界临时值，再 conflict
    # 回 EXTREME，最后 clamp。若实现每步提前 clamp（boost→clamp→EXTREME→
    # conflict→VERY_HIGH），结果会错成 VERY_HIGH——此测试拦截这种实现。
    r = derive_confidence(
        [
            _item(EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST, "a"),
            _item(EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST, "b"),
        ],
        now=_NOW,
        conflicting_pairs=[("a", "b")],
    )
    assert r.tier == ConfidenceTier.EXTREME
    assert r.has_conflict is True
    assert "independence_boost" in r.applied_rules
    assert "conflict_penalty" in r.applied_rules
    assert "clamp" in r.applied_rules


# --- 4. 新鲜度：严格早于边界才 stale；恰好边界新鲜 ---------------------------


def test_all_strictly_older_is_stale() -> None:
    r = derive_confidence(
        [_item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a", observed_at=_NOW - _WINDOW - timedelta(seconds=1))],
        now=_NOW,
    )
    assert r.is_stale is True
    assert r.tier == ConfidenceTier.LOW  # LOW_MID -1
    assert "staleness_penalty" in r.applied_rules


def test_exactly_boundary_is_fresh() -> None:
    r = derive_confidence(
        [_item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a", observed_at=_NOW - _WINDOW)],
        now=_NOW,
    )
    assert r.is_stale is False
    assert r.tier == ConfidenceTier.LOW_MID


def test_one_fresh_means_not_stale() -> None:
    r = derive_confidence(
        [
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a", observed_at=_NOW - _WINDOW - timedelta(days=1)),
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "b", observed_at=_NOW),
        ],
        now=_NOW,
    )
    assert r.is_stale is False
    assert r.tier == ConfidenceTier.MID  # 2 独立 → +1；非全 stale → 无过期下浮


# --- 5. 钳制 ------------------------------------------------------------------


def test_extreme_clamped_on_boost() -> None:
    r = derive_confidence(
        [
            _item(EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST, "a"),
            _item(EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST, "b"),
        ],
        now=_NOW,
    )
    assert r.tier == ConfidenceTier.EXTREME
    assert "independence_boost" in r.applied_rules
    assert "clamp" in r.applied_rules


def test_low_clamped_on_conflict_and_staleness() -> None:
    r = derive_confidence(
        [
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "a", observed_at=_NOW - _WINDOW - timedelta(days=1)),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "b", observed_at=_NOW - _WINDOW - timedelta(days=1)),
        ],
        now=_NOW,
        conflicting_pairs=[("a", "b")],
    )
    assert r.tier == ConfidenceTier.LOW
    assert r.has_conflict is True
    assert r.is_stale is True
    assert "clamp" in r.applied_rules


# --- 6. 输入校验 --------------------------------------------------------------


def test_empty_evidence_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        derive_confidence([], now=_NOW)


@pytest.mark.parametrize("window", [timedelta(0), timedelta(days=-1)])
def test_non_positive_freshness_window_raises(window: timedelta) -> None:
    with pytest.raises(ValidationError):
        derive_confidence([_item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a")], now=_NOW, freshness_window=window)


# --- 7. explanation 与 applied_rules ------------------------------------------


def test_explanation_mentions_base_level_and_base_tier() -> None:
    r = derive_confidence([_item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a")], now=_NOW)
    assert r.explanation
    assert EvidenceLevel.PUBLIC_COMPANY_EVENT.value in r.explanation
    assert ConfidenceTier.LOW_MID.value in r.explanation  # 基准档位
    assert "base_from_highest" in r.applied_rules


def test_explanation_mentions_boost_and_conflict_rule_names() -> None:
    # 组合用例：两条独立 PUBLIC + 相互冲突 → base LOW_MID、boost +1、conflict -1。
    # explanation 需以中文叙述并带稳定规则名（实现约定：括号内规则名）。
    r = derive_confidence(
        [
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a"),
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "b"),
        ],
        now=_NOW,
        conflicting_pairs=[("a", "b")],
    )
    assert r.explanation
    assert EvidenceLevel.PUBLIC_COMPANY_EVENT.value in r.explanation
    assert ConfidenceTier.LOW_MID.value in r.explanation
    assert "independence_boost" in r.explanation
    assert "conflict_penalty" in r.explanation


def test_explanation_lists_highest_summaries_in_input_order() -> None:
    # 两条不同摘要按输入顺序出现（index 比较）——输出必须是确定性、非哈希序。
    first = "Acme 宣布扩建第二座工厂"
    second = "采购经理岗位新增招聘"
    r = derive_confidence(
        [
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a", summary=first),
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "b", summary=second),
        ],
        now=_NOW,
    )
    assert first in r.explanation
    assert second in r.explanation
    assert r.explanation.index(first) < r.explanation.index(second)


def test_explanation_dedups_repeated_summary() -> None:
    # 相同摘要（即使不同 source_id）只出现一次。
    summary = "Acme 宣布扩建第二座工厂"
    r = derive_confidence(
        [
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "a", summary=summary),
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "b", summary=summary),
        ],
        now=_NOW,
    )
    assert r.explanation.count(summary) == 1


def test_explanation_does_not_mix_lower_level_summary() -> None:
    # 低等级证据的摘要不得冒充基准（最高等级层）。
    r = derive_confidence(
        [
            _item(EvidenceLevel.PUBLIC_COMPANY_EVENT, "high", summary="高等级证据摘要"),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "low", summary="低等级证据摘要"),
        ],
        now=_NOW,
    )
    assert "高等级证据摘要" in r.explanation
    assert "低等级证据摘要" not in r.explanation


def test_applied_rules_use_fixed_names() -> None:
    r = derive_confidence(
        [
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "a", observed_at=_NOW - _WINDOW - timedelta(days=1)),
            _item(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "b", observed_at=_NOW - _WINDOW - timedelta(days=1)),
        ],
        now=_NOW,
        conflicting_pairs=[("a", "b")],
    )
    assert r.applied_rules
    for name in r.applied_rules:
        assert name in _FIXED_RULE_NAMES


# --- 8. meets_threshold：大于 / 等于 / 小于 -----------------------------------


def _result(tier: ConfidenceTier) -> ConfidenceResult:
    return ConfidenceResult(
        tier=tier,
        has_conflict=False,
        is_stale=False,
        explanation="x",
        applied_rules=["base_from_highest"],
    )


def test_meets_threshold_greater() -> None:
    assert meets_threshold(_result(ConfidenceTier.HIGH), ConfidenceTier.MID_HIGH) is True


def test_meets_threshold_equal() -> None:
    assert meets_threshold(_result(ConfidenceTier.HIGH), ConfidenceTier.HIGH) is True


def test_meets_threshold_less() -> None:
    assert meets_threshold(_result(ConfidenceTier.LOW), ConfidenceTier.MID) is False
    assert meets_threshold(_result(ConfidenceTier.HIGH), ConfidenceTier.EXTREME) is False
