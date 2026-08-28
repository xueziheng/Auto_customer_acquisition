"""公开摘要逐字段保留商业信息，但不能泄露内部依据和来源原文。"""

import importlib
import json
import subprocess
import sys
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.unit.test_need_units import bound_facts
from tests.unit.test_quotation_contracts import (
    detail_fixture,
    expense_case,
    frozen_fixture,
    quote_fx_case,
)
from tests.unit.test_quote_context_contracts import issuer


@pytest.mark.parametrize("domain", ["demand", "costing", "quotations"])
@pytest.mark.parametrize("module", ["http_schemas", "http_projection"])
def test_http_contract_modules_import_independently(domain, module):
    result = subprocess.run(
        [sys.executable, "-c", f"import domains.{domain}.{module}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def projection(domain):
    name = f"domains.{domain}.http_projection"
    assert importlib.util.find_spec(name), "缺少安全HTTP白名单投影"
    return importlib.import_module(name)


@pytest.mark.parametrize(
    "domain,names",
    [
        ("demand", ("project_need_unit_preparation", "project_need_unit_confirmation")),
        (
            "costing",
            (
                "project_policy",
                "project_price_evidence",
                "project_quote_fx",
                "project_coverage",
                "project_scope",
            ),
        ),
        ("quotations", ("project_issuer", "project_need", "project_internal_quote")),
    ],
)
def test_workflow_can_use_projections_through_public_domain_service(domain, names):
    public = importlib.import_module(f"domains.{domain}.service")
    for name in names:
        assert hasattr(public, name), f"缺少公共纯投影 {domain}.{name}"
        assert getattr(public, name) is getattr(projection(domain), name)


def assert_no_raw(value):
    serialized = value.model_dump_json()
    for marker in (
        '"source_quote":',
        '"source_url":',
        '"need_facts":',
        '"basis":',
        '"runtime":',
    ):
        assert marker not in serialized
    assert "locator" not in value.model_dump()
    if "source" in value.model_dump() and value.source is not None:
        assert "locator" not in value.source.model_dump()


def test_need_preparation_keeps_bound_identity_without_raw_source():
    result = projection("demand").project_need_unit_preparation(bound_facts())
    assert result.quantity == 500 and result.unit == "pieces"
    assert result.unit_confirmation_id == "nuc_first"
    assert result.quantity_status == result.unit_status == "current"
    assert_no_raw(result)


async def test_unit_confirmation_public_receipt_has_only_safe_source_identity():
    from tests.unit.test_need_units import MemoryCase, confirm, unit_service

    module = projection("demand")
    assert hasattr(module, "project_need_unit_confirmation"), "缺少单位确认安全回执"
    original = await confirm(unit_service(MemoryCase()))
    result = module.project_need_unit_confirmation(original)
    assert result.confirmation_id == original.confirmation_id
    assert result.unit == original.unit.value
    assert result.source_message_id == original.source.source_message_id
    assert result.artifact_id == original.source.artifact_id
    assert result.content_hash == original.source.content_hash
    assert_no_raw(result)


@pytest.mark.parametrize(
    "domain,names",
    [
        ("demand", ("NeedUnitPreparationView", "NeedUnitConfirmationPublicView")),
        (
            "costing",
            (
                "CostCalculationCommand",
                "PricingPolicyPublicView",
                "PriceEvidencePublicView",
                "QuoteFxPublicView",
                "CostCoveragePublicView",
                "CostScopePublicView",
            ),
        ),
        (
            "quotations",
            (
                "QuoteEmptyCommand",
                "QuoteIssuerPublicView",
                "QuoteNeedPublicSummary",
                "QuotePreparationPublicView",
                "QuoteInternalPublicView",
            ),
        ),
    ],
)
def test_http_dtos_export_same_class_at_domain_public_boundary(domain, names):
    schema = importlib.import_module(f"domains.{domain}.schemas")
    service = importlib.import_module(f"domains.{domain}.service")
    http = importlib.import_module(f"domains.{domain}.http_schemas")
    for name in names:
        assert hasattr(schema, name) and hasattr(service, name), (
            f"缺少公共契约 {domain}.{name}"
        )
        assert getattr(schema, name) is getattr(service, name) is getattr(http, name)


def test_empty_quote_command_rejects_control_fields():
    from domains.quotations import schemas

    assert hasattr(schemas, "QuoteEmptyCommand"), "缺少严格空命令"
    assert schemas.QuoteEmptyCommand.model_validate({}).model_dump() == {}
    for key in ("force", "actor", "approved", "history", "template", "key"):
        with pytest.raises(ValidationError):
            schemas.QuoteEmptyCommand.model_validate({key: True})


def test_provenance_summary_normalizes_utc_and_rejects_invalid_confirmers():
    from datetime import timedelta, timezone

    from shared.schemas.provenance import ProvenanceSummary
    from tests.unit.test_need_units import field

    raw = (
        projection("demand")
        .project_need_unit_preparation(bound_facts())
        .quantity_origin.model_dump()
    )
    raw["confirmed_at"] = raw["extracted_at"] = field(
        1
    ).provenance.extracted_at.astimezone(timezone(timedelta(hours=8)))
    actual = ProvenanceSummary.model_validate(raw)
    assert actual.confirmed_at.utcoffset() == timedelta(0)
    for actor in ("", " emp_test", "emp_test\x00", "x" * 41):
        with pytest.raises(ValidationError):
            ProvenanceSummary.model_validate({**raw, "confirmed_by": actor})


async def test_unit_receipt_public_times_require_aware_and_preserve_json_wire():
    from tests.unit.test_need_units import MemoryCase, confirm, unit_service

    original = projection("demand").project_need_unit_confirmation(
        await confirm(unit_service(MemoryCase()))
    )
    assert type(original).model_validate_json(original.model_dump_json()) == original
    for name in ("confirmed_at", "observed_at"):
        with pytest.raises(ValidationError):
            type(original).model_validate(
                {
                    **original.model_dump(),
                    name: original.confirmed_at.replace(tzinfo=None),
                }
            )


def test_issuer_summary_keeps_manual_confirmation_without_raw_source():
    result = projection("quotations").project_issuer(issuer())
    assert result.name == "Supplier Company"
    assert result.field_provenance["name"].source_type.value == "employee_input"
    assert_no_raw(result)


def test_internal_quote_projects_real_nested_content_and_scope():
    original = detail_fixture()
    result = projection("quotations").project_internal_quote(original)
    assert result.quote_id == original.content.quote_id
    assert result.state == original.state
    assert (
        result.scope_confirmation_id
        == original.content.basis.scope_confirmation.confirmation_id
    )
    assert result.calculation == original.content.basis.calculation
    assert result.lines == original.content.lines
    assert_no_raw(result)


def test_supplier_projection_preserves_quantity_range_and_money():
    original = frozen_fixture().price_evidence[0]
    result = projection("costing").project_price_evidence(original)
    assert result.kind == "supplier_price"
    assert result.amount == original.amount
    assert (result.quantity_min, result.quantity_max, result.moq) == (
        original.quantity_min,
        original.quantity_max,
        original.moq,
    )
    assert "quantity" not in result.model_dump()
    assert result.valid_until == original.valid_until
    assert "source_quote" not in result.model_dump_json()
    assert "locator" not in result.model_dump()
    assert "locator" not in result.source.model_dump()
    assert set(result.field_provenance["locator"].model_dump()) == {
        "source_type",
        "source_id",
        "extracted_by",
        "extracted_at",
        "confirmed_by",
        "confirmed_at",
    }


def test_expense_projection_keeps_actual_and_nullable_expiry():
    from domains.costing.schemas import ExpenseEvidenceView

    raw = json.loads(expense_case()[1].price_evidence[1].model_dump_json())
    raw.pop("tenant_id")
    raw["currency"], raw["amount"] = raw["amount"]["currency"], raw["amount"]["amount"]
    raw["valid_until"] = None
    original = ExpenseEvidenceView.model_validate_json(json.dumps(raw))
    result = projection("costing").project_price_evidence(original)
    assert result.kind == "confirmed_expense" and result.basis == "actual"
    assert result.valid_until is None
    assert "moq" not in result.model_dump() and "unit" not in result.model_dump()
    assert result.quantity == original.quantity and result.amount == original.amount


@pytest.mark.parametrize(
    "field,projector",
    [
        ("policy", "project_policy"),
        ("coverage", "project_coverage"),
        ("scope_confirmation", "project_scope"),
    ],
)
def test_costing_confirmation_summaries_do_not_expose_original_facts(field, projector):
    original = getattr(frozen_fixture(), field)
    module = projection("costing")
    assert hasattr(module, projector), "缺少确认资料安全投影"
    result = getattr(module, projector)(original)
    assert result.content_hash == original.content_hash
    assert_no_raw(result)


def test_quote_fx_summary_preserves_direction_and_decimal():
    from domains.costing.schemas import QuoteFxView

    original = QuoteFxView.model_validate_json(
        quote_fx_case()[1].quote_fx.model_dump_json()
    )
    module = projection("costing")
    assert hasattr(module, "project_quote_fx"), "缺少汇率安全投影"
    result = module.project_quote_fx(original)
    assert (result.base_currency, result.quote_currency, result.rate) == (
        original.base_currency,
        original.quote_currency,
        original.rate,
    )
    assert_no_raw(result)


def test_calculation_wire_accepts_only_explicit_options_without_embedded_fx():
    from domains.costing import schemas

    assert hasattr(schemas, "CostCalculationCommand"), "缺少测算HTTP安全命令"
    raw = {
        "mode": "manual",
        "unit_price": {"amount": "2.30", "currency": "USD"},
        "rounding": {"unit_places": 2, "total_places": 2, "strategy": "ROUND_HALF_UP"},
        "quote_fx_ref": None,
        "algorithm_version": "costing-v1",
    }
    command = schemas.CostCalculationCommand.model_validate_json(json.dumps(raw))
    assert command.unit_price.amount == Decimal("2.30")
    for change in (
        {"quote_fx": {}},
        {"mode": "target"},
        {"unit_price": {"amount": 2.3, "currency": "USD"}},
    ):
        with pytest.raises(ValidationError):
            schemas.CostCalculationCommand.model_validate_json(json.dumps(raw | change))
