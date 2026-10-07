"""shared.schemas.money 的契约单测（行为断言；窄 cast 仅用于故意传错误运行时类型）。

硬边界 2：金额一律 Decimal、禁 float。错误 taxonomy：公共输入错 → ValidationError；
币种方向不匹配 → CurrencyMismatchError。
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

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


def test_pydantic_money_json_preserves_decimal_string_without_accepting_number() -> None:
    """若 JSON number 先变 float，高精度金额会在进入 Money 前被截断。"""
    adapter = TypeAdapter(Money)
    amount = "0.123456789012345678901234"

    parsed = adapter.validate_json(
        '{"amount":"0.123456789012345678901234","currency":"USD"}'
    )
    assert parsed.amount == Decimal(amount)
    with pytest.raises(PydanticValidationError):
        adapter.validate_json('{"amount":0.123456789012345678901234,"currency":"USD"}')
    with pytest.raises(PydanticValidationError):
        adapter.validate_python({"amount": amount, "currency": "USD"})


@pytest.mark.parametrize("amount", [1, 1.0, True, "1.0"])
def test_pydantic_money_python_rejects_non_decimal_amount(amount: object) -> None:
    """Python TypeAdapter 不能把 int/float/bool/string 隐式变成金额 Decimal。"""
    with pytest.raises(PydanticValidationError):
        TypeAdapter(Money).validate_python({"amount": amount, "currency": "USD"})


@pytest.mark.parametrize("amount", ["1", "1.0", "true", "null"])
def test_pydantic_money_json_rejects_non_string_amount_tokens(amount: str) -> None:
    """JSON number/bool/null 不得越过 wire Decimal 字符串边界。"""
    with pytest.raises(PydanticValidationError):
        TypeAdapter(Money).validate_json(
            ('{"amount":' + amount + ',"currency":"USD"}').encode()
        )


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
def test_pydantic_money_json_rejects_non_finite_decimal_string(amount: str) -> None:
    """wire 字符串即使能被 Decimal 解析，非有限值仍不得进入金额契约。"""
    with pytest.raises(PydanticValidationError):
        TypeAdapter(Money).validate_json(
            ('{"amount":"' + amount + '","currency":"USD"}').encode()
        )


def test_pydantic_money_and_fx_rate_decimal_schema_is_string_only() -> None:
    """若 schema 宣称 number，客户端会再次发送会被拒绝或丢精度的 JSON number。"""
    money_amount = TypeAdapter(Money).json_schema()["properties"]["amount"]
    fx_rate = TypeAdapter(FxRate).json_schema()["properties"]["rate"]

    assert money_amount["type"] == "string"
    assert fx_rate["type"] == "string"
    assert "number" not in str(money_amount)
    assert "number" not in str(fx_rate)


@pytest.mark.parametrize("amount", ["abc", "  ", "", "+", "."])
def test_pydantic_money_malformed_json_decimal_string_is_validation_error(
    amount: str,
) -> None:
    """若 Decimal 解析异常逸出，HTTP middleware 无法将坏输入安全映射为 400。"""
    adapter = TypeAdapter(Money)
    payload = ('{"amount":' + repr(amount).replace("'", '"') + ',"currency":"USD"}').encode()

    with pytest.raises(PydanticValidationError):
        adapter.validate_json(payload)


@pytest.mark.parametrize("rate", ["abc", "  ", "", "+", "."])
def test_pydantic_fx_rate_malformed_json_decimal_string_is_validation_error(
    rate: str,
) -> None:
    """汇率与金额共享同一 wire Decimal 边界，坏字符串不得泄漏 Decimal 异常。"""
    adapter = TypeAdapter(FxRate)
    payload = (
        '{"base":"USD","quote":"CNY","rate":'
        + repr(rate).replace("'", '"')
        + ',"observed_at":"2026-08-08T00:00:00Z","source":"fx"}'
    ).encode()

    with pytest.raises(PydanticValidationError):
        adapter.validate_json(payload)


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
