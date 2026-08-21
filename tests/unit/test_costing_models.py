from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money

_models = importlib.import_module("domains.costing.models")
CostItem = _models.CostItem
CostItemType = _models.CostItemType
CostSheet = _models.CostSheet
CostSheetVersion = _models.CostSheetVersion
RiskAcceptance = _models.RiskAcceptance


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
        source_ref="price-snapshot-one" if confirmed else None,
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
        fx_snapshot_id=FxSnapshotId("fx-snapshot-one"),
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


def test_confirmed_cost_item_requires_valid_basis_amount_and_provenance() -> None:
    with pytest.raises(ValidationError, match="价格基准"):
        _item(CostItemType.PRODUCT_PURCHASE, price_basis="guessed")
    with pytest.raises(ValidationError, match="不能为负"):
        CostItem(
            item_type=CostItemType.PRODUCT_PURCHASE,
            amount=Money(Decimal("-0.01"), CurrencyCode("USD")),
            price_basis="quoted",
            is_per_unit=True,
            source_ref="price-snapshot-one",
            entered_by=EmployeeId("employee-one"),
        )
    with pytest.raises(ValidationError, match="来源引用"):
        CostItem(
            item_type=CostItemType.PRODUCT_PURCHASE,
            amount=Money(Decimal("1.00"), CurrencyCode("USD")),
            price_basis="quoted",
            is_per_unit=True,
            entered_by=EmployeeId("employee-one"),
        )


def test_quoted_cost_sheet_requires_positive_dimensions_and_fx_snapshot() -> None:
    sheet = _sheet(_item(CostItemType.PRODUCT_PURCHASE))
    values = vars(sheet).copy()
    values["fx_snapshot_id"] = None
    with pytest.raises(ValidationError, match="汇率快照"):
        CostSheet(**values)
    values = vars(sheet).copy()
    values["quantity"] = 0
    with pytest.raises(ValidationError, match="数量"):
        CostSheet(**values)


def test_risk_acceptance_requires_auditable_human_justification() -> None:
    with pytest.raises(ValidationError, match="理由"):
        RiskAcceptance(
            accepted_by=EmployeeId("employee-one"),
            accepted_at=datetime(2026, 8, 21, 10, tzinfo=UTC),
            justification=" ",
        )
