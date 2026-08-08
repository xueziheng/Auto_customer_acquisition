"""shared.schemas.money 的契约单测（行为断言；窄 cast 仅用于故意传错误运行时类型）。

硬边界 2：金额一律 Decimal、禁 float。错误 taxonomy：公共输入错 → ValidationError；
币种方向不匹配 → CurrencyMismatchError。
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from shared.errors import CurrencyMismatchError, ValidationError
from shared.schemas.money import CurrencyCode, FxRate, Money, PriceBasis, convert

_USD = CurrencyCode("USD")
_CNY = CurrencyCode("CNY")
_JPY = CurrencyCode("JPY")
_NOW = datetime(2026, 8, 8, tzinfo=UTC)


# --- Money：amount 必须是有限 Decimal ---------------------------------------


def test_money_accepts_finite_decimal() -> None:
    m = Money(Decimal("5000.00"), _USD)
    assert m.amount == Decimal("5000.00")
    assert m.currency == _USD


@pytest.mark.parametrize("bad_amount", [cast(Decimal, 1.0), cast(Decimal, 5000), cast(Decimal, True)])
def test_money_rejects_float_int_bool_amount(bad_amount: Decimal) -> None:
    with pytest.raises(ValidationError):
        Money(bad_amount, _USD)


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_money_rejects_non_finite_decimal_amount(bad: str) -> None:
    with pytest.raises(ValidationError):
        Money(Decimal(bad), _USD)


@pytest.mark.parametrize("bad", ["US", "usd", "US1", "USDO", "ÅBC"])
def test_money_rejects_invalid_currency(bad: str) -> None:
    with pytest.raises(ValidationError):
        Money(Decimal(1), cast(CurrencyCode, bad))


# --- add：同币种相加，异币种抛 CurrencyMismatchError ------------------------


def test_add_same_currency() -> None:
    result = Money(Decimal("1.50"), _USD).add(Money(Decimal("2.50"), _USD))
    assert result == Money(Decimal("4.00"), _USD)


def test_add_currency_mismatch() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(Decimal(1), _USD).add(Money(Decimal(1), _CNY))


# --- multiply：只乘 Decimal/int（含负/零），拒绝 float/bool/Money/非有限 ----


def test_multiply_decimal_and_int() -> None:
    base = Money(Decimal("2.00"), _USD)
    assert base.multiply(Decimal(3)) == Money(Decimal("6.00"), _USD)
    assert base.multiply(3) == Money(Decimal("6.00"), _USD)
    assert base.multiply(Decimal(-2)) == Money(Decimal("-4.00"), _USD)
    assert base.multiply(0) == Money(Decimal("0.00"), _USD)


@pytest.mark.parametrize(
    "bad_factor",
    [cast(Decimal, 1.5), cast(Decimal, True), Decimal("Infinity"), Decimal("NaN")],
)
def test_multiply_rejects_bad_factors(bad_factor: Decimal) -> None:
    with pytest.raises(ValidationError):
        Money(Decimal("2.00"), _USD).multiply(bad_factor)


def test_multiply_rejects_money_factor() -> None:
    with pytest.raises(ValidationError):
        Money(Decimal("2.00"), _USD).multiply(cast(Decimal, Money(Decimal(1), _USD)))


# --- round_to：显式精度与策略 ----------------------------------------------


def test_round_default_half_up() -> None:
    assert Money(Decimal("1.005"), _USD).round_to(2) == Money(Decimal("1.01"), _USD)


def test_round_custom_strategy() -> None:
    assert Money(Decimal("1.005"), _USD).round_to(2, "ROUND_DOWN") == Money(Decimal("1.00"), _USD)


def test_round_zero_places() -> None:
    assert Money(Decimal("2.50"), _USD).round_to(0) == Money(Decimal(3), _USD)


@pytest.mark.parametrize("bad_places", [cast(int, True), cast(int, 2.0), -1])
def test_round_rejects_bad_places(bad_places: int) -> None:
    with pytest.raises(ValidationError):
        Money(Decimal("1.005"), _USD).round_to(bad_places)


def test_round_rejects_unknown_strategy() -> None:
    with pytest.raises(ValidationError):
        Money(Decimal("1.005"), _USD).round_to(2, "ROUND_XYZ")


# --- FxRate：rate 有限正 Decimal、币种合法、source 非空、同币种仅 rate==1 ----


def test_fxrate_valid_cross_currency() -> None:
    r = FxRate(_USD, _CNY, Decimal("7.2"), _NOW, "fx-data-source")
    assert r.rate == Decimal("7.2")


def test_fxrate_valid_same_currency_rate_one() -> None:
    FxRate(_USD, _USD, Decimal(1), _NOW, "fx-data-source")  # 同币种归一化快照合法


@pytest.mark.parametrize(
    "bad_rate",
    [cast(Decimal, 7.2), Decimal(0), Decimal(-1), Decimal("Infinity"), Decimal("NaN")],
)
def test_fxrate_rejects_bad_rate(bad_rate: Decimal) -> None:
    with pytest.raises(ValidationError):
        FxRate(_USD, _CNY, bad_rate, _NOW, "fx-data-source")


def test_fxrate_rejects_same_currency_rate_not_one() -> None:
    with pytest.raises(ValidationError):
        FxRate(_USD, _USD, Decimal(2), _NOW, "fx-data-source")


def test_fxrate_rejects_invalid_base() -> None:
    with pytest.raises(ValidationError):
        FxRate(cast(CurrencyCode, "usd"), _CNY, Decimal("7.2"), _NOW, "fx-data-source")


def test_fxrate_rejects_invalid_quote() -> None:
    with pytest.raises(ValidationError):
        FxRate(_USD, cast(CurrencyCode, "cny"), Decimal("7.2"), _NOW, "fx-data-source")


@pytest.mark.parametrize("blank", ["", "   "])
def test_fxrate_rejects_blank_source(blank: str) -> None:
    with pytest.raises(ValidationError):
        FxRate(_USD, _CNY, Decimal("7.2"), _NOW, blank)


# --- convert：方向精确、不取倒数、不舍入 ------------------------------------


def test_convert_exact_no_rounding() -> None:
    # 3 × 7.2345 = 21.7035：若实现错误地舍入到 2 位（21.70），数值即不相等，断言会失败。
    rate = FxRate(_USD, _CNY, Decimal("7.2345"), _NOW, "fx-data-source")
    result = convert(Money(Decimal(3), _USD), _CNY, rate)
    assert result == Money(Decimal("21.7035"), _CNY)


def test_convert_base_mismatch() -> None:
    rate = FxRate(_CNY, _JPY, Decimal(20), _NOW, "fx-data-source")
    with pytest.raises(CurrencyMismatchError):
        convert(Money(Decimal(100), _USD), _JPY, rate)


def test_convert_quote_mismatch() -> None:
    rate = FxRate(_USD, _JPY, Decimal(150), _NOW, "fx-data-source")
    with pytest.raises(CurrencyMismatchError):
        convert(Money(Decimal(100), _USD), _CNY, rate)


# --- PriceBasis：硬边界 7 常量 ----------------------------------------------


def test_price_basis_constants() -> None:
    assert PriceBasis.INDICATIVE == "indicative"
    assert PriceBasis.QUOTED == "quoted"
