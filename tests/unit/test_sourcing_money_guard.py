"""Unicode-aware sourcing money classification contract."""

from __future__ import annotations

import sys
import unicodedata

import pytest

from agent_runtime.sourcing_agent.money_guard import contains_untrusted_money


@pytest.mark.parametrize(
    "text",
    [
        "AED250",
        "AED—250",
        "250/AED",
        "aed 250",
        "250\u00a0aed",
        "250\u2009/AED",
        "Grade 304 costs AED 250",
        "Model X。 AED 250 is listed",
    ],
)
def test_currency_and_number_pairs_reject_outside_identity(text: str) -> None:
    assert contains_untrusted_money(text, trusted_literals=()) is True


@pytest.mark.parametrize(
    "text", ["RMB250", "ＡＥＤ２５０", "250 per piece", "250/piece"]
)
def test_currency_aliases_and_integer_per_unit_forms_reject(text: str) -> None:
    assert contains_untrusted_money(text, trusted_literals=()) is True


@pytest.mark.parametrize(
    "text",
    [
        "Model AED 250",
        "Model AED-250",
        "Series no CHF250",
        "Code 250/AED",
        "Grade 304",
        "Model AED 250 uses grade 304 steel",
    ],
)
def test_strict_identity_spans_can_protect_currency_shaped_identifiers(
    text: str,
) -> None:
    assert contains_untrusted_money(text, trusted_literals=()) is False


def test_identity_composition_rejects_incomplete_grade_prose() -> None:
    assert (
        contains_untrusted_money("Model AED 250 uses grade steel", trusted_literals=())
        is True
    )


_UNICODE_CURRENCY_SYMBOLS = tuple(
    chr(codepoint)
    for codepoint in range(sys.maxunicode + 1)
    if unicodedata.category(chr(codepoint)) == "Sc"
)


@pytest.mark.parametrize("symbol", _UNICODE_CURRENCY_SYMBOLS)
def test_every_unicode_currency_symbol_with_digits_rejects(symbol: str) -> None:
    assert len(_UNICODE_CURRENCY_SYMBOLS) == 63
    assert (
        contains_untrusted_money(f"Observed {symbol} 250 today", trusted_literals=())
        is True
    )


@pytest.mark.parametrize("decimal", [".50", "2,50", "１２．５０", "１２，５０", "٢٫٥٠"])
def test_untrusted_unicode_decimal_spans_reject(decimal: str) -> None:
    assert (
        contains_untrusted_money(f"Tolerance is {decimal} mm", trusted_literals=())
        is True
    )


@pytest.mark.parametrize("decimal", [".50", "2,50", "１２．５０", "１２，５０", "٢٫٥٠"])
def test_exact_trusted_unicode_decimal_spans_pass(decimal: str) -> None:
    literal = f"Tolerance {decimal} mm"
    assert (
        contains_untrusted_money(
            f"The required {literal} applies", trusted_literals=(literal,)
        )
        is False
    )


def test_trusted_decimal_occurrence_does_not_cover_a_second_occurrence() -> None:
    assert (
        contains_untrusted_money(
            "Tolerance 2.50 mm is required, another 2.50 is listed",
            trusted_literals=("Tolerance 2.50 mm",),
        )
        is True
    )


def test_trusted_decimal_does_not_cross_sentence_boundaries() -> None:
    assert (
        contains_untrusted_money(
            "Tolerance 2.50 mm. The other value is 2.50",
            trusted_literals=("Tolerance 2.50 mm",),
        )
        is True
    )


@pytest.mark.parametrize(
    "text",
    [
        "cost will be 250",
        "price starts from 2.50",
        "Grade 304 costs AED 250",
        "cost impact is approximately 250",
        "cost implication: 250",
        "cost impact is approximately 2.50",
    ],
)
def test_price_cost_predicates_and_targeted_exemptions_reject(text: str) -> None:
    assert (
        contains_untrusted_money(
            text,
            trusted_literals=("2.50", "250", "grade 304"),
        )
        is True
    )


@pytest.mark.parametrize(
    "text",
    [
        "amount is about 304 pieces",
        "amount of 304 stainless steel",
        "The 304 cost impact is unknown",
        "cost impact for grade 304 steel is unknown",
        "price-sensitive Model AED-250",
        "Model AED 250 uses grade 304 steel",
    ],
)
def test_closed_non_money_phrases_pass(text: str) -> None:
    assert (
        contains_untrusted_money(
            text,
            trusted_literals=("304", "grade 304 steel"),
        )
        is False
    )


def test_untrusted_amount_decimal_rejects_without_money_keyword() -> None:
    assert contains_untrusted_money("amount 2.50", trusted_literals=()) is True


@pytest.mark.parametrize(
    "text",
    [
        "x" * 16_001,
        "safe\x00text",
        "safe\ud800text",
        "safe\u202etext",
    ],
)
def test_invalid_or_adversarial_text_fails_closed(text: str) -> None:
    assert contains_untrusted_money(text, trusted_literals=()) is True


def test_unbounded_or_malformed_trusted_literals_fail_closed() -> None:
    assert (
        contains_untrusted_money("safe text", trusted_literals=("x" * 4_001,)) is True
    )
    assert (
        contains_untrusted_money("safe text", trusted_literals=("bad\ud800literal",))
        is True
    )
