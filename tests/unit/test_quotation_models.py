from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shared.schemas.identifiers import (
    CostSheetId,
    OpportunityId,
    QuoteId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money

_models = importlib.import_module("domains.quotations.models")
Quote = _models.Quote
QuoteState = _models.QuoteState

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _quote(*, state: QuoteState = QuoteState.DRAFT) -> Quote:
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
