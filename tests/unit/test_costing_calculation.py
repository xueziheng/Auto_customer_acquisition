from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from domains.costing.errors import EmptyCostSheetError, MissingFxSnapshotError
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, FxRate, Money

_service = importlib.import_module("domains.costing.service")
_models = importlib.import_module("domains.costing.models")
CostItem = _models.CostItem
CostItemType = _models.CostItemType
CostSheet = _models.CostSheet
CostSheetVersion = _models.CostSheetVersion


def _item(
    amount: str,
    currency: str,
    *,
    per_unit: bool,
    confirmed: bool = True,
) -> CostItem:
    return CostItem(
        item_type=CostItemType.PRODUCT_PURCHASE,
        amount=Money(Decimal(amount), CurrencyCode(currency)),
        price_basis="quoted",
        is_per_unit=per_unit,
        source_ref="price-snapshot-one" if confirmed else None,
        entered_by=EmployeeId("employee-one") if confirmed else None,
    )


def _sheet(
    *items: CostItem,
    quantity: int = 100,
    fx_rates: tuple[FxRate, ...] = (),
) -> CostSheet:
    return CostSheet(
        cost_sheet_id=CostSheetId("cost-sheet-one"),
        tenant_id=TenantId("tenant-one"),
        opportunity_id=OpportunityId("opportunity-one"),
        version_type=CostSheetVersion.QUOTED,
        version_number=1,
        quantity=quantity,
        base_currency="USD",
        quote_currency="USD",
        created_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
        items=list(items),
        fx_snapshot_id=FxSnapshotId("fx-snapshot-one"),
        fx_rates=fx_rates,
    )


def _rate(base: str, quote: str, value: str) -> FxRate:
    return FxRate(
        base=CurrencyCode(base),
        quote=CurrencyCode(quote),
        rate=Decimal(value),
        observed_at=datetime(2026, 8, 23, 9, tzinfo=UTC),
        source="manual-fx-snapshot",
    )


def test_unit_full_cost_distributes_order_cost_and_ignores_unconfirmed_suggestions() -> None:
    sheet = _sheet(
        _item("1.25", "USD", per_unit=True),
        _item("25", "USD", per_unit=False),
        _item("999", "USD", per_unit=True, confirmed=False),
    )

    result = _service.compute_unit_full_cost(sheet)

    assert result == Money(Decimal("1.50"), CurrencyCode("USD"))


def test_unit_full_cost_uses_only_direct_rates_from_locked_snapshot() -> None:
    sheet = _sheet(
        _item("7", "CNY", per_unit=True),
        _item("0.02", "USD", per_unit=True),
        fx_rates=(_rate("CNY", "USD", "0.14"),),
    )

    result = _service.compute_unit_full_cost(sheet)

    assert result == Money(Decimal("1.00"), CurrencyCode("USD"))


def test_unit_full_cost_rejects_missing_currency_rate() -> None:
    sheet = _sheet(_item("7", "CNY", per_unit=True))

    with pytest.raises(MissingFxSnapshotError, match="CNY.*USD"):
        _service.compute_unit_full_cost(sheet)


def test_unit_full_cost_rejects_sheet_without_confirmed_items() -> None:
    sheet = _sheet(_item("1", "USD", per_unit=True, confirmed=False))

    with pytest.raises(EmptyCostSheetError, match="确认"):
        _service.compute_unit_full_cost(sheet)


def test_cost_sheet_rejects_duplicate_or_misdirected_snapshot_rates() -> None:
    with pytest.raises(ValidationError, match="汇率快照"):
        _sheet(
            _item("1", "USD", per_unit=True),
            fx_rates=(
                _rate("CNY", "USD", "0.14"),
                _rate("CNY", "USD", "0.15"),
            ),
        )

    with pytest.raises(ValidationError, match="核算币种"):
        _sheet(
            _item("1", "USD", per_unit=True),
            fx_rates=(_rate("CNY", "EUR", "0.13"),),
        )
