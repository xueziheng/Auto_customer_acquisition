"""S2-8 打分纯函数单测（GateResult / check_gates / compute_score / rank_bucket / ScoringPolicy）。

行为断言，不依赖实现细节：
- ``GateResult.all_passed``：无失败即通过。
- ``check_gates`` 四门全部检查不短路、各失败 reason 具体；金额未知写「预计金额未知」；
  证据低于 LOW_MID 失败；estimated 与 minimum 币种不一致抛 ``CurrencyMismatchError``。
- ``compute_score`` 返回 ``SortKey``：tier rank LOW=1…EXTREME=7（显式稳定、与 7 枚举一致）；
  value_band=严格小于的升序边界数（**等于边界不视为越过**）；supply_rank False/None/True→0/1/2；
  estimated 与 policy 边界币种不一致抛 ``CurrencyMismatchError``。
- ``rank_bucket`` 只由 evidence_rank 查注入 policy。
- ``SortKey`` 证据主导 tuple 序。
- ``ScoringPolicy`` 空/非升序/异币种/不完整或非法 bucket_map 负测。

禁止 float/加权/对数/总分；策略全部注入；不引入业务阈值。不重复 shared 推导逻辑。
RED：scoring 新符号（rank_bucket/_TIER_RANK）经 importlib 延迟导入转行为失败。
"""
from __future__ import annotations

import importlib
from decimal import Decimal

import pytest

from domains.opportunities.scoring import (
    Gate,
    GateResult,
    ScoringInput,
    ScoringPolicy,
    SortKey,
    check_gates,
    compute_score,
)
from shared.errors import CurrencyMismatchError, ValidationError
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.money import CurrencyCode, Money

_USD = CurrencyCode("USD")
_CNY = CurrencyCode("CNY")
_100 = Money(Decimal(100), _USD)
_1000 = Money(Decimal(1000), _USD)

_MODULE_BY_SYMBOL = {
    "rank_bucket": "domains.opportunities.scoring",
    "_TIER_RANK": "domains.opportunities.scoring",
}


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED 阶段新符号未建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _full_map() -> dict[int, str]:
    return {i: "low" for i in range(1, 6)} | {6: "mid", 7: "high"}


def _policy(
    *,
    version: str = "gates-v1",
    value_band_boundaries: tuple[Money, ...] = (_100, _1000),
    bucket_map: dict[int, str] | None = None,
) -> ScoringPolicy:
    return ScoringPolicy(
        version=version,
        value_band_boundaries=value_band_boundaries,
        bucket_map=bucket_map if bucket_map is not None else _full_map(),
    )


# --- GateResult ----------------------------------------------------------------


def test_gate_result_all_passed() -> None:
    ok = GateResult(passed=[Gate.CONTACTABLE], failed=[], reasons={})
    assert ok.all_passed is True
    bad = GateResult(
        passed=[], failed=[Gate.CONTACTABLE], reasons={Gate.CONTACTABLE.value: "x"}
    )
    assert bad.all_passed is False


# --- check_gates ----------------------------------------------------------------


def test_check_gates_no_short_circuit() -> None:
    """四门全部检查不短路：全失败时 four failed 且各 reason 具体。"""
    input = ScoringInput(
        has_verified_contact=False,
        evidence_tier=ConfidenceTier.LOW,
        category_allowed=False,
        minimum_order_value=_100,
        estimated_order_value=None,
    )
    result = check_gates(input)
    assert set(result.failed) == {
        Gate.CONTACTABLE,
        Gate.EVIDENCE_SUFFICIENT,
        Gate.VALUE_ABOVE_FLOOR,
        Gate.CATEGORY_ALLOWED,
    }
    assert result.all_passed is False
    assert result.reasons[Gate.VALUE_ABOVE_FLOOR.value] == "预计金额未知"
    assert result.reasons[Gate.CONTACTABLE.value]
    assert result.reasons[Gate.EVIDENCE_SUFFICIENT.value]
    assert result.reasons[Gate.CATEGORY_ALLOWED.value]


def test_value_above_floor_none_reason() -> None:
    """预计金额未知 → VALUE_ABOVE_FLOOR 未通过，reason 具体。"""
    input = ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.HIGH,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=None,
    )
    result = check_gates(input)
    assert Gate.VALUE_ABOVE_FLOOR in result.failed
    assert result.reasons[Gate.VALUE_ABOVE_FLOOR.value] == "预计金额未知"


def test_evidence_below_minimum_fails() -> None:
    """证据低于 LOW_MID → 不通过；等于 LOW_MID 通过。"""
    low = ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.LOW,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(500), _USD),
    )
    assert Gate.EVIDENCE_SUFFICIENT in check_gates(low).failed

    ok = ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.LOW_MID,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(500), _USD),
    )
    assert check_gates(ok).all_passed is True


def test_check_gates_currency_mismatch_raises() -> None:
    """estimated 与 minimum 币种不一致 → CurrencyMismatchError。"""
    input = ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.HIGH,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(500), _CNY),
    )
    with pytest.raises(CurrencyMismatchError):
        check_gates(input)


# --- compute_score --------------------------------------------------------------


def test_compute_score_returns_sortkey() -> None:
    """compute_score 返回 SortKey：HIGH=5、越过 100/1000 两边界=2、True→2。"""
    input = ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.HIGH,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(1500), _USD),
        supply_available=True,
    )
    assert compute_score(input, _policy()) == SortKey(5, 2, 2)


def test_compute_score_value_band_equality_boundary() -> None:
    """等于边界不视为越过：value == 1000 → value_band 只计严格小于的边界。"""
    input = ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.MID,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(1000), _USD),
        supply_available=None,
    )
    assert compute_score(input, _policy()) == SortKey(3, 1, 1)  # 100<1000 计 1；None→1


def test_compute_score_supply_rank_mapping() -> None:
    """supply_rank：False→0、None→1、True→2。"""
    def _score(supply: bool | None) -> SortKey:
        return compute_score(
            ScoringInput(
                has_verified_contact=True,
                evidence_tier=ConfidenceTier.MID,
                category_allowed=True,
                minimum_order_value=_100,
                estimated_order_value=Money(Decimal(500), _USD),
                supply_available=supply,
            ),
            _policy(),
        )

    assert _score(False).supply_rank == 0
    assert _score(None).supply_rank == 1
    assert _score(True).supply_rank == 2


def test_compute_score_boundary_currency_mismatch() -> None:
    """estimated 与 policy 边界币种不一致 → CurrencyMismatchError。"""
    input = ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.HIGH,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(1500), _CNY),
    )
    with pytest.raises(CurrencyMismatchError):
        compute_score(input, _policy())


def test_tier_rank_is_stable_1_to_7() -> None:
    """档位映射显式稳定：LOW=1…EXTREME=7，与 ConfidenceTier 七枚举一致。"""
    _TIER_RANK = _load("_TIER_RANK")
    assert _TIER_RANK[ConfidenceTier.LOW] == 1
    assert _TIER_RANK[ConfidenceTier.LOW_MID] == 2
    assert _TIER_RANK[ConfidenceTier.MID] == 3
    assert _TIER_RANK[ConfidenceTier.MID_HIGH] == 4
    assert _TIER_RANK[ConfidenceTier.HIGH] == 5
    assert _TIER_RANK[ConfidenceTier.VERY_HIGH] == 6
    assert _TIER_RANK[ConfidenceTier.EXTREME] == 7
    assert set(_TIER_RANK) == set(ConfidenceTier)


# --- rank_bucket / SortKey 序 ----------------------------------------------------


def test_rank_bucket_from_policy() -> None:
    """rank_bucket 只由 evidence_rank 查注入 policy（不临场算档位）。"""
    rank_bucket = _load("rank_bucket")
    policy = _policy()
    assert rank_bucket(7, policy) == "high"
    assert rank_bucket(6, policy) == "mid"
    assert rank_bucket(3, policy) == "low"


def test_sortkey_tuple_ordering() -> None:
    """SortKey 证据主导：HIGH 压过 LOW 无论价值/供应；同证据按价值/供应。"""
    assert SortKey(7, 0, 0) > SortKey(1, 9, 2)
    assert SortKey(3, 2, 0) < SortKey(3, 2, 1)


# --- ScoringPolicy 构造负测 --------------------------------------------------------


def test_policy_rejects_bad_boundaries() -> None:
    with pytest.raises(ValidationError):
        ScoringPolicy(version="x", value_band_boundaries=(), bucket_map=_full_map())  # 空
    with pytest.raises(ValidationError):
        ScoringPolicy(version="x", value_band_boundaries=(_1000, _100), bucket_map=_full_map())  # 非升序
    with pytest.raises(ValidationError):
        ScoringPolicy(  # 异币种
            version="x",
            value_band_boundaries=(_100, Money(Decimal(200), _CNY)),
            bucket_map=_full_map(),
        )


def test_policy_rejects_bad_bucket_map() -> None:
    with pytest.raises(ValidationError):
        ScoringPolicy(version="x", value_band_boundaries=(_100,), bucket_map={1: "low", 2: "mid"})  # 未覆盖 1..7
    with pytest.raises(ValidationError):
        ScoringPolicy(  # 非法值
            version="x",
            value_band_boundaries=(_100,),
            bucket_map={i: "bad" for i in range(1, 8)},
        )
