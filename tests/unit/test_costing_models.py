from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal

from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money

_models = importlib.import_module("domains.costing.models")
CostItem = _models.CostItem
CostItemType = _models.CostItemType
CostSheet = _models.CostSheet
CostSheetVersion = _models.CostSheetVersion


def _item(
    item_type: CostItemType,
    *,
    price_basis: str = "quoted",
    confirmed: bool = True,
) -> CostItem:
    return CostItem(
        item_type=item_type,
        amount=Money(Decimal("1.25"), CurrencyCode("USD")),
        price_basis=price_basis,
        is_per_unit=True,
        entered_by=EmployeeId("employee-one") if confirmed else None,
    )


def _sheet(*items: CostItem) -> CostSheet:
    return CostSheet(
        cost_sheet_id=CostSheetId("cost-sheet-one"),
        tenant_id=TenantId("tenant-one"),
        opportunity_id=OpportunityId("opportunity-one"),
        version_type=CostSheetVersion.QUOTED,
        version_number=1,
        quantity=100,
        base_currency="USD",
        quote_currency="USD",
        created_at=datetime(2026, 8, 21, 10, tzinfo=UTC),
        items=list(items),
    )


def test_indicative_gate_only_considers_human_confirmed_items() -> None:
    pending = _item(
        CostItemType.PRODUCT_PURCHASE,
        price_basis="indicative",
        confirmed=False,
    )
    confirmed = _item(
        CostItemType.PACKAGING,
        price_basis="indicative",
        confirmed=True,
    )

    assert _sheet(pending).has_indicative_items() is False
    assert _sheet(pending, confirmed).has_indicative_items() is True


def test_missing_item_types_ignores_pending_model_values_and_preserves_order() -> None:
    sheet = _sheet(
        _item(CostItemType.PRODUCT_PURCHASE),
        _item(CostItemType.PACKAGING, confirmed=False),
    )

    assert sheet.missing_item_types(
        [
            CostItemType.PACKAGING,
            CostItemType.PRODUCT_PURCHASE,
            CostItemType.PACKAGING,
            CostItemType.INTERNATIONAL_FREIGHT,
        ]
    ) == [
        CostItemType.PACKAGING,
        CostItemType.INTERNATIONAL_FREIGHT,
    ]
