"""报价依据输入边界；HTTP fixture 使用实际解码后的字符串。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from domains.costing import schemas
from domains.costing.service import cost_item_type_values
from shared.schemas.money import Money

NOW = datetime(2026, 8, 28, 9, tzinfo=UTC)


def schema(name: str):
    value = getattr(schemas, name, None)
    assert value is not None, f"报价依据契约 {name} 未实现"
    return value


def expense_payload(**changes: object) -> dict[str, object]:
    return {
        "kind": "confirmed_expense",
        "opportunity_id": "opp-one",
        "item_type": "packaging",
        "allocation_scope": "order:opp-one",
        "is_per_unit": False,
        "quantity": 100,
        "currency": "USD",
        "basis": "actual",
        "source_ref": "art-one",
        "locator": "page:1/line:2",
        "amount": "12.50",
        "observed_at": "2026-08-27T09:00:00Z",
        "valid_until": None,
        **changes,
    }


def supplier_payload(**changes: object) -> dict[str, object]:
    return {
        "kind": "supplier_price",
        "opportunity_id": "opp-one",
        "need_id": "need-one",
        "supplier_ref": "supplier:one",
        "specification": "hinge-304",
        "unit": "piece",
        "destination": "US",
        "currency": "USD",
        "basis": "quoted",
        "source_ref": "art-one",
        "locator": "page:1/line:1",
        "quantity_min": 100,
        "quantity_max": 500,
        "moq": 100,
        "amount": "1.20",
        "quoted_at": "2026-08-27T09:00:00Z",
        "valid_until": "2026-09-27T09:00:00Z",
        **changes,
    }


def policy_payload(**changes: object) -> dict[str, object]:
    return {
        "category": None,
        "minimum_margin_rate": "0.10",
        "target_margin_rate": "0.20",
        "cost_groups": {name: "goods" for name in cost_item_type_values()},
        "effective_from": "2026-08-27T09:00:00Z",
        "source_ref": "art-policy",
        **changes,
    }


def test_expense_http_input_retains_actual_without_supplier_moq() -> None:
    value = schema("ExpenseEvidenceCreate").model_validate(expense_payload())
    assert value.amount == Decimal("12.50")
    assert value.observed_at == datetime(2026, 8, 27, 9, tzinfo=UTC)
    assert value.basis == "actual"
    assert "moq" not in value.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"source_ref": " "},
        {"allocation_scope": ""},
        {"confirmed_by": "boss"},
        {"confirmed_at": "2026-08-28T09:00:00Z"},
        {"tenant_id": "other"},
        {"amount": 1.2},
        {"amount": True},
        {"amount": "NaN"},
        {"amount": "-1"},
        {"amount": "0.0000000000001"},
        {"amount": "10000000000000000"},
        {"basis": "indicative"},
        {"item_type": "product_purchase"},
        {"item_type": "invented_fee"},
        {"moq": 100},
        {"locator": " "},
        {"observed_at": "2026-08-27T09:00:00"},
        {"valid_until": "2026-08-26T09:00:00Z"},
        {"quantity": 0},
        {"is_per_unit": "false"},
    ],
)
def test_expense_rejects_untrusted_or_inexact_input(changes: dict[str, object]) -> None:
    model = schema("ExpenseEvidenceCreate")
    with pytest.raises(ValidationError):
        model.model_validate(expense_payload(**changes))


def test_supplier_http_quoted_scope_is_explicit() -> None:
    value = schema("SupplierPriceEvidenceCreate").model_validate(supplier_payload())
    assert (value.quantity_min, value.quantity_max, value.moq) == (100, 500, 100)
    assert value.amount == Decimal("1.20")


@pytest.mark.parametrize(
    "field", ["quantity_min", "quantity_max", "moq", "valid_until", "source_ref"]
)
def test_supplier_cannot_omit_required_quote_scope(field: str) -> None:
    model = schema("SupplierPriceEvidenceCreate")
    payload = supplier_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity_min": 0},
        {"quantity_max": 99},
        {"moq": 501},
        {"quantity_min": True},
        {"valid_until": "2026-08-26T09:00:00Z"},
        {"unit": ""},
        {"destination": " "},
        {"confirmed_by": "boss"},
        {"currency": "usd"},
        {"basis": "actual"},
    ],
)
def test_supplier_rejects_invalid_business_scope(changes: dict[str, object]) -> None:
    model = schema("SupplierPriceEvidenceCreate")
    with pytest.raises(ValidationError):
        model.model_validate(supplier_payload(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed_by": "boss"},
        {"cost_groups": {"product_purchase": "goods"}},
        {"minimum_margin_rate": "0.1000000000001"},
    ],
)
def test_policy_input_has_no_client_confirmation_or_missing_groups(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        schemas.PricingPolicyCreate.model_validate(policy_payload(**changes))


@pytest.mark.parametrize(
    "value", ["1.0000000000000", "0E-100", "9999999999999999.9999999999990"]
)
def test_cost_precision_accepts_lossless_trailing_zeros_even_in_small_context(
    value: str,
) -> None:
    models = importlib.import_module("domains.costing.models")
    with localcontext() as context:
        context.prec = 3
        item = models.CostItem(
            item_type=models.CostItemType.PACKAGING,
            amount=Money(Decimal(value), "USD"),
            price_basis="actual",
            is_per_unit=False,
        )
    assert item.amount.amount == Decimal(value)


def test_quote_fx_http_contract_keeps_its_direction() -> None:
    value = schema("QuoteFxCreate").model_validate(
        {
            "base_currency": "CNY",
            "quote_currency": "USD",
            "source_ref": "art-fx",
            "rate": "0.1400000000000",
            "observed_at": "2026-08-27T09:00:00Z",
        }
    )
    assert value.rate == Decimal("0.14")
    assert (value.base_currency, value.quote_currency) == ("CNY", "USD")


def coverage_payload(**changes: object) -> dict[str, object]:
    return {
        "expected_sheet_hash": "a" * 64,
        "acquisition_mode": "detail",
        "decisions": [
            {
                "item_type": name,
                "applicable": False,
                "reason": "本订单明确不适用",
                "item_bindings": [],
            }
            for name in cost_item_type_values()
        ],
        **changes,
    }


def test_coverage_http_lists_preserve_explicit_not_applicable_decisions() -> None:
    result = schema("CostCoverageCreate").model_validate(coverage_payload())
    assert len(result.decisions) == 22
    assert result.decisions[0].reason == "本订单明确不适用"


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "reason", "amount", "hidden_binding", "sequence"],
)
def test_coverage_cannot_turn_missing_costs_into_zero(mutation: str) -> None:
    model = schema("CostCoverageCreate")
    payload = coverage_payload()
    if mutation == "missing":
        payload["decisions"].pop()
    elif mutation == "duplicate":
        payload["decisions"][-1] = payload["decisions"][0]
    elif mutation == "reason":
        payload["decisions"][0]["reason"] = " "
    elif mutation == "amount":
        payload["decisions"][0]["applicable"] = True
    else:
        payload["decisions"][0]["item_bindings"] = [
            {
                "item_sequence": 0 if mutation == "sequence" else 1,
                "evidence_id": "price-one",
                "source_line_ref": "line:1",
                "allocation_scope": "order:one",
            }
        ]
        if mutation == "sequence":
            payload["decisions"][0]["applicable"] = True
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_web_source_requires_its_real_url_and_hash() -> None:
    model = schema("SourceEvidence")
    payload = {
        "tenant_id": "tenant-one",
        "source_ref": "art-one",
        "artifact_id": "art-one",
        "content_hash": "a" * 64,
        "locator": "line:1",
        "observed_at": NOW,
        "source_type": "web_page",
    }
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    source = model.model_validate(
        {**payload, "source_url": "https://supplier.example/price"}
    )
    assert source.source_type == "web_page"
    assert source.source_url == "https://supplier.example/price"


def test_sheet_identity_includes_sources_but_not_lock_time() -> None:
    from dataclasses import replace

    models = importlib.import_module("domains.costing.models")
    service = importlib.import_module("domains.costing.service_impl")
    item = models.CostItem(
        item_type=models.CostItemType.PACKAGING,
        amount=Money(Decimal(2), "USD"),
        price_basis="actual",
        is_per_unit=False,
        source_ref="art-one",
        entered_by="emp-one",
        item_sequence=7,
    )
    sheet = models.CostSheet(
        cost_sheet_id="sheet-one",
        tenant_id="tenant-one",
        opportunity_id="opp-one",
        version_type=models.CostSheetVersion.ESTIMATED,
        version_number=1,
        quantity=100,
        base_currency="USD",
        quote_currency="USD",
        created_at=NOW,
        items=[item],
    )
    view = service._view(sheet)
    assert view.items[0].item_sequence == 7
    assert len(view.content_hash) == 64
    sheet.locked_at = NOW
    assert service._view(sheet).content_hash == view.content_hash
    sheet.items = [replace(item, source_ref="art-two")]
    assert service._view(sheet).content_hash != view.content_hash


def test_public_cost_item_view_preserves_existing_positional_note() -> None:
    view = schemas.CostItemView(
        "packaging", "包装", Money(Decimal(2), "USD"), "actual", False, "原备注"
    )
    assert view.note == "原备注"


def test_public_cost_sheet_view_preserves_existing_positional_fx_snapshot() -> None:
    view = schemas.CostSheetView(
        "sheet", "opp", "quoted", 1, 100, "USD", "EUR", [], NOW, False, False, "fx-old"
    )
    assert view.fx_snapshot_id == "fx-old"
