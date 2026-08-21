from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    OpportunityId,
    QuoteId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money

_models = importlib.import_module("domains.quotations.models")
Quote = _models.Quote
QuoteLine = _models.QuoteLine
QuoteState = _models.QuoteState

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _quote(*, state: QuoteState = QuoteState.DRAFT) -> Quote:
    line = QuoteLine(
        line_number=1,
        description="Stainless steel hinge",
        quantity=1000,
        unit_price=Money(Decimal("1.25"), CurrencyCode("USD")),
        line_total=Money(Decimal("1250.00"), CurrencyCode("USD")),
        price_snapshot_ref="price-snapshot-one",
    )
    return Quote(
        quote_id=QuoteId("quote-one"),
        tenant_id=TenantId("tenant-one"),
        opportunity_id=OpportunityId("opportunity-one"),
        version=1,
        currency="USD",
        total=Money(Decimal("1250.00"), CurrencyCode("USD")),
        valid_until=NOW,
        cost_sheet_id=CostSheetId("cost-sheet-one"),
        created_at=NOW - timedelta(days=1),
        lines=[line],
        state=state,
    )


def test_quote_transition_follows_approval_state_machine_without_bypass() -> None:
    draft = _quote()
    approved = _quote(state=QuoteState.APPROVED)
    terminal = _quote(state=QuoteState.ACCEPTED)

    assert draft.can_transition_to(QuoteState.PENDING_APPROVAL) is True
    assert draft.can_transition_to(QuoteState.SENT) is False
    assert approved.can_transition_to(QuoteState.SENT) is True
    assert terminal.can_transition_to(QuoteState.SUPERSEDED) is False


def test_quote_expires_when_validity_instant_is_reached() -> None:
    quote = _quote()

    assert quote.is_expired_at(NOW - timedelta(microseconds=1)) is False
    assert quote.is_expired_at(NOW) is True
    assert quote.is_expired_at(NOW + timedelta(days=1)) is True


def test_quote_line_requires_exact_decimal_total_and_snapshot_reference() -> None:
    with pytest.raises(ValidationError, match="报价行总额"):
        QuoteLine(
            line_number=1,
            description="Hinge",
            quantity=3,
            unit_price=Money(Decimal("0.10"), CurrencyCode("USD")),
            line_total=Money(Decimal("0.31"), CurrencyCode("USD")),
            price_snapshot_ref="price-snapshot-one",
        )
    with pytest.raises(ValidationError, match="价格快照引用"):
        QuoteLine(
            line_number=1,
            description="Hinge",
            quantity=1,
            unit_price=Money(Decimal("1.00"), CurrencyCode("USD")),
            line_total=Money(Decimal("1.00"), CurrencyCode("USD")),
            price_snapshot_ref=" ",
        )


def test_quote_requires_lines_matching_total_currency_and_validity_window() -> None:
    valid = _quote()
    base = {
        "quote_id": valid.quote_id,
        "tenant_id": valid.tenant_id,
        "opportunity_id": valid.opportunity_id,
        "version": valid.version,
        "currency": valid.currency,
        "total": valid.total,
        "valid_until": valid.valid_until,
        "cost_sheet_id": valid.cost_sheet_id,
        "created_at": valid.created_at,
        "lines": valid.lines,
    }

    with pytest.raises(ValidationError, match="至少一行"):
        Quote(**{**base, "lines": []})
    with pytest.raises(ValidationError, match="报价总额"):
        Quote(
            **{
                **base,
                "total": Money(Decimal("1200.00"), CurrencyCode("USD")),
            }
        )
    with pytest.raises(ValidationError, match="有效期"):
        Quote(**{**base, "valid_until": valid.created_at})
