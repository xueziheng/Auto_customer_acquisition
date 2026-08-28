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
RiskAcceptance = _models.RiskAcceptance


def _item(
    amount: str,
    currency: str,
    *,
    per_unit: bool,
    confirmed: bool = True,
    price_basis: str = "quoted",
) -> CostItem:
    return CostItem(
        item_type=CostItemType.PRODUCT_PURCHASE,
        amount=Money(Decimal(amount), CurrencyCode(currency)),
        price_basis=price_basis,
        is_per_unit=per_unit,
        source_ref="price-snapshot-one" if confirmed else None,
        entered_by=EmployeeId("employee-one") if confirmed else None,
    )


def _sheet(
    *items: CostItem,
    quantity: int = 100,
    fx_rates: tuple[FxRate, ...] = (),
    version_type: CostSheetVersion = CostSheetVersion.QUOTED,
    risk_acceptance: RiskAcceptance | None = None,
) -> CostSheet:
    return CostSheet(
        cost_sheet_id=CostSheetId("cost-sheet-one"),
        tenant_id=TenantId("tenant-one"),
        opportunity_id=OpportunityId("opportunity-one"),
        version_type=version_type,
        version_number=1,
        quantity=quantity,
        base_currency="USD",
        quote_currency="USD",
        created_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
        items=list(items),
        fx_snapshot_id=(
            FxSnapshotId("fx-snapshot-one")
            if version_type is CostSheetVersion.QUOTED
            else None
        ),
        fx_rates=fx_rates,
        risk_acceptance=risk_acceptance,
    )


def _rate(base: str, quote: str, value: str) -> FxRate:
    return FxRate(
        base=CurrencyCode(base),
        quote=CurrencyCode(quote),
        rate=Decimal(value),
        observed_at=datetime(2026, 8, 23, 9, tzinfo=UTC),
        source="manual-fx-snapshot",
    )


@pytest.mark.parametrize("mode", ["target", "manual"])
def test_same_currency_does_not_accept_unrelated_identity_fx(mode: str) -> None:
    """汇率值为一不代表其币种对适用于本成本表。"""
    from types import SimpleNamespace

    calc = importlib.import_module("domains.costing.calculation")
    sheet = SimpleNamespace(base_currency="USD", quote_currency="USD")
    options = SimpleNamespace(quote_fx=_rate("EUR", "CNY", "1"))
    with pytest.raises(ValidationError):
        if mode == "target":
            calc._base_to_quote_price(Decimal("2"), sheet=sheet, options=options)
        else:
            calc._quote_to_base_revenue(Money(Decimal("2"), "USD"), sheet=sheet, options=options)


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


def test_quote_readiness_accepts_confirmed_quoted_same_currency_costs() -> None:
    readiness = _service.assess_quote_readiness(
        _sheet(_item("1", "USD", per_unit=True)),
        expected_item_types=[CostItemType.PRODUCT_PURCHASE],
    )

    assert readiness.ready is True
    assert readiness.blockers == []
    assert readiness.indicative_items == []
    assert readiness.missing_items == []


def test_quote_readiness_reports_all_deterministic_blockers_at_once() -> None:
    indicative = CostItem(
        item_type=CostItemType.PRODUCT_PURCHASE,
        amount=Money(Decimal(7), CurrencyCode("CNY")),
        price_basis="indicative",
        is_per_unit=True,
        source_ref="public-price-snapshot",
        entered_by=EmployeeId("employee-one"),
    )
    sheet = _sheet(indicative)

    readiness = _service.assess_quote_readiness(
        sheet,
        expected_item_types=[
            CostItemType.PRODUCT_PURCHASE,
            CostItemType.PACKAGING,
        ],
    )

    assert readiness.ready is False
    assert readiness.blockers == [
        "含参考价成本项，必须取得供应商实报价或完成人工风险接受",
        "汇率快照缺少 CNY 到 USD 的直连汇率",
        "成本表缺少业务场景要求的成本项",
    ]
    assert readiness.indicative_items == ["product_purchase"]
    assert readiness.missing_items == ["packaging"]


def test_quote_readiness_requires_quoted_version_and_confirmed_values() -> None:
    readiness = _service.assess_quote_readiness(
        _sheet(
            _item("1", "USD", per_unit=True, confirmed=False),
            version_type=CostSheetVersion.ESTIMATED,
        )
    )

    assert readiness.ready is False
    assert readiness.blockers == [
        "只有 QUOTED 版本可以锁定用于客户报价",
        "成本表没有已确认成本项",
        "QUOTED 成本表必须绑定汇率快照",
    ]


def test_quote_readiness_honors_audited_indicative_risk_acceptance() -> None:
    indicative = CostItem(
        item_type=CostItemType.PRODUCT_PURCHASE,
        amount=Money(Decimal(1), CurrencyCode("USD")),
        price_basis="indicative",
        is_per_unit=True,
        source_ref="public-price-snapshot",
        entered_by=EmployeeId("employee-one"),
    )
    readiness = _service.assess_quote_readiness(
        _sheet(
            indicative,
            risk_acceptance=RiskAcceptance(
                accepted_by=EmployeeId("boss-one"),
                accepted_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
                justification="客户明确要求紧急预算，已逐次审批接受参考价风险。",
            ),
        )
    )

    assert readiness.ready is True
    assert readiness.indicative_items == ["product_purchase"]


def test_quote_readiness_never_accepts_actual_cost_as_supplier_quote() -> None:
    readiness = _service.assess_quote_readiness(
        _sheet(
            _item(
                "1",
                "USD",
                per_unit=True,
                price_basis=CostSheetVersion.ACTUAL.value,
            )
        )
    )

    assert readiness.ready is False
    assert readiness.blockers == [
        "含 actual 成本基准；客户报价只允许 quoted，indicative 仅可经人工风险接受"
    ]
