"""不可变报价新契约与旧模型保持独立；所有来源都是受控样例。"""

import importlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domains.quotations import schemas as q
from domains.quotations import service as public
from shared.schemas.money import Money
from shared.schemas.quote_creation import QuoteRoundingInput
from tests.unit.test_quote_context_contracts import business_context, intent

COST_TYPES = (
    "product_purchase",
    "sample_fee",
    "mold_fee",
    "customization_fee",
    "logo_printing",
    "packaging",
    "quality_inspection",
    "wastage",
    "domestic_freight",
    "international_freight",
    "insurance",
    "customs_clearance",
    "duties_and_taxes",
    "destination_freight",
    "warehousing",
    "payment_fees",
    "sales_commission",
    "customer_acquisition",
    "contact_data_cost",
    "ad_allocation",
    "agent_api_allocation",
    "returns_reserve",
)
SUPPLIER_FIELDS = (
    "opportunity_id",
    "amount",
    "currency",
    "source_ref",
    "locator",
    "kind",
    "need_id",
    "supplier_ref",
    "specification",
    "unit",
    "destination",
    "basis",
    "quantity_min",
    "quantity_max",
    "moq",
    "quoted_at",
    "valid_until",
)
EXPENSE_FIELDS = (
    "opportunity_id",
    "amount",
    "currency",
    "source_ref",
    "locator",
    "kind",
    "item_type",
    "allocation_scope",
    "is_per_unit",
    "quantity",
    "basis",
    "observed_at",
    "valid_until",
)
FX_FIELDS = ("base_currency", "quote_currency", "source_ref", "rate", "observed_at")
POLICY_FIELDS = (
    "category",
    "minimum_margin_rate",
    "target_margin_rate",
    "effective_from",
    "source_ref",
)


def coverage_provenance(decisions, provenance):
    """合成fixture按已读上游确认输入补齐叶字段，不调用报价门禁的实现。"""
    keys = {"expected_sheet_hash", "acquisition_mode", "cost_sheet_id"}
    for index, decision in enumerate(decisions):
        prefix = f"decisions.{index}"
        keys.update(
            f"{prefix}.{name}" for name in ("item_type", "applicable", "reason")
        )
        if not decision.item_bindings:
            keys.add(f"{prefix}.item_bindings")
        for binding_index in range(len(decision.item_bindings)):
            keys.update(
                f"{prefix}.item_bindings.{binding_index}.{name}"
                for name in (
                    "item_sequence",
                    "evidence_id",
                    "source_line_ref",
                    "allocation_scope",
                )
            )
    return {key: provenance for key in keys}


def contracts():
    assert hasattr(q, "QuoteContentLine"), "缺少不可变报价内容契约"
    return q


def line(**changes):
    return contracts().QuoteContentLine(
        **(
            {
                "line_number": 1,
                "description": "Part",
                "specification": "Material: Steel",
                "unit": "piece",
                "quantity": 3,
                "unit_price": Money(Decimal("1.235"), "USD"),
                "line_total": Money(Decimal("3.71"), "USD"),
                "rounding": QuoteRoundingInput(
                    unit_places=3, total_places=2, strategy="ROUND_HALF_UP"
                ),
            }
            | changes
        )
    )


def test_new_line_rounding_and_shared_customer_projection_contract():
    value = line()
    assert value.line_total.amount == Decimal("3.71")
    from shared.schemas.quote_document import CustomerQuoteView

    assert q.CustomerQuoteView is CustomerQuoteView


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": True},
        {"line_number": 2},
        {"line_total": Money(Decimal("3.70"), "USD")},
        {"unit": " piece"},
        {"approved": True},
    ],
)
def test_line_rejects_invalid_shape_and_amount(changes):
    with pytest.raises((ValidationError, ValueError)):
        line(**changes)


def test_wire_money_requires_decimal_strings():
    value = line()
    raw = json.loads(value.model_dump_json())
    assert raw["unit_price"]["amount"] == "1.235"
    assert q.QuoteContentLine.model_validate_json(json.dumps(raw)) == value
    raw["unit_price"]["amount"] = 1.235
    with pytest.raises(ValidationError):
        q.QuoteContentLine.model_validate_json(json.dumps(raw))


def draft(**changes):
    c = contracts()
    original = intent()
    data = {name: getattr(original, name) for name in c.QuoteDraftCommand.model_fields}
    return c.QuoteDraftCommand(**(data | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_quote_version": True},
        {"replaces_quote_id": "q_old"},
        {"valid_until": intent().valid_until.replace(tzinfo=None)},
        {"prepared_by": "emp_other"},
        {"unit_price": Money(Decimal("1E-13"), "USD")},
        {"unit_price": Money(Decimal("1E+100000"), "USD")},
    ],
)
def test_draft_is_strict_and_has_no_trusted_client_fields(changes):
    contracts()
    from domains.quotations.errors import QuotationError

    with pytest.raises((ValidationError, QuotationError)):
        draft(**changes)


def test_specification_uses_all_confirmed_dimensions_in_fixed_order():
    contracts()
    spec = q.QuoteSpecificationFacts(
        product_category="Hinges",
        application="Doors",
        material="Steel",
        size_spec="50mm",
        packaging="Cartons",
        certification_required="CE",
    )
    assert public.format_quote_specification(spec) == (
        "Product category: Hinges\nApplication: Doors\nMaterial: Steel\nSize: 50mm\n"
        "Packaging: Cartons\nCertification required: CE"
    )


def basis_case():
    """完整受控快照用于纯规则；真实冻结真实性在集成测试验证。"""
    from dataclasses import replace

    from shared.schemas.provenance import SourceType
    from shared.schemas.quote_creation import (
        quote_creation_request_hash,
        quote_terms_hash,
    )
    from tests.unit.test_need_units import NOW, field

    c = business_context()
    i = intent(expected_context_hash=c.context_hash)
    p = replace(
        field("raw evidence").provenance,
        source_type=SourceType.UPLOAD,
        source_id="artifact_test",
    )
    source = q.QuoteEvidenceSource(
        tenant_id=c.tenant_id,
        source_ref="artifact_test",
        artifact_id="artifact_test",
        content_hash="d" * 64,
        locator="line:1",
        observed_at=NOW,
        source_type="upload",
        source_url=None,
    )
    common = {"source": source, "confirmed_by": i.prepared_by, "confirmed_at": NOW}
    supplier_values = {
        "kind": "supplier_price",
        "tenant_id": c.tenant_id,
        "opportunity_id": c.opportunity_id,
        "evidence_id": "ev_test",
        "evidence_hash": "e" * 64,
        "source_ref": "artifact_test",
        "locator": "line:1",
        "amount": Money(Decimal("1.25"), "USD"),
        "need_id": c.need_id,
        "supplier_ref": "supplier",
        "specification": "Supplier free text",
        "unit": c.unit,
        "destination": c.destination,
        "basis": "quoted",
        "quantity_min": 100,
        "quantity_max": 1000,
        "moq": 100,
        "quoted_at": NOW,
        "valid_until": i.valid_until + timedelta(days=1),
    }
    provenance_keys = set(supplier_values) - {
        "tenant_id",
        "evidence_id",
        "evidence_hash",
    }
    provenance_keys.add("currency")
    price = q.QuoteSupplierEvidence(
        **supplier_values, **common, field_provenance={k: p for k in provenance_keys}
    )
    policy = q.QuotePolicySnapshot(
        policy_id="policy_test",
        content_hash="d" * 64,
        category="hinges",
        minimum_margin_rate=Decimal("0.1"),
        target_margin_rate=Decimal("0.2"),
        cost_groups={
            name: "goods" if name == "product_purchase" else "fixed"
            for name in COST_TYPES
        },
        effective_from=NOW,
        source_ref="artifact_test",
        source=source,
        confirmed_by=i.prepared_by,
        confirmed_at=NOW,
        field_provenance={
            k: p
            for k in (*POLICY_FIELDS, *(f"cost_groups.{name}" for name in COST_TYPES))
        },
    )
    coverage = q.QuoteCoverageSnapshot(
        expected_sheet_hash=i.expected_sheet_hash,
        decisions=(
            q.QuoteCoverageDecision(
                item_type="product_purchase",
                applicable=True,
                reason="confirmed",
                item_bindings=(
                    q.QuoteCostItemBinding(
                        item_sequence=1,
                        evidence_id=price.evidence_id,
                        source_line_ref="line:1",
                        allocation_scope="order:one",
                    ),
                ),
            ),
        )
        + tuple(
            q.QuoteCoverageDecision(
                item_type=name,
                applicable=False,
                reason="not applicable",
                item_bindings=(),
            )
            for name in COST_TYPES
            if name != "product_purchase"
        ),
        acquisition_mode="detail",
        coverage_id="a" * 64,
        cost_sheet_id=i.cost_sheet_id,
        content_hash="a" * 64,
        confirmed_by=i.prepared_by,
        confirmed_at=NOW,
        field_provenance={},
    )
    coverage = coverage.model_copy(
        update={"field_provenance": coverage_provenance(coverage.decisions, p)}
    )
    scope = q.QuoteScopeConfirmation(
        tenant_id=c.tenant_id,
        confirmation_id=i.scope_confirmation_id,
        opportunity_id=c.opportunity_id,
        need_id=c.need_id,
        cost_sheet_id=i.cost_sheet_id,
        sheet_hash=i.expected_sheet_hash,
        coverage_id=coverage.coverage_id,
        coverage_hash=coverage.content_hash,
        need_facts=c.need_facts,
        need_facts_hash=c.need_facts_hash,
        specification=c.specification,
        specification_hash=c.specification_hash,
        terms=i.terms,
        terms_hash=quote_terms_hash(i.terms),
        valid_until=i.valid_until,
        evidence_bindings=(
            q.QuoteScopeEvidenceBinding(
                evidence_id=price.evidence_id,
                evidence_hash=price.evidence_hash,
                applicability_note="confirmed all dimensions",
            ),
        ),
        content_hash=i.scope_confirmation_hash,
        provenance=p,
    )
    metrics = q.QuoteProfitMetrics(
        **{name: Decimal("0.01") for name in q.QuoteProfitMetrics.model_fields}
    )
    calculation = q.QuoteCalculationSnapshot(
        cost_sheet_id=i.cost_sheet_id,
        policy_id=policy.policy_id,
        inputs_hash="a" * 64,
        context_hash=c.context_hash,
        version_number=1,
        computed_at=NOW,
        base_currency="USD",
        quote_currency="USD",
        metrics=metrics,
        effective_unit_revenue=i.unit_price,
        displayed_unit_price=i.unit_price,
        displayed_total=Money(Decimal("1150.00"), "USD"),
    )
    basis = q.QuoteBasis(
        tenant_id=c.tenant_id,
        basis_id="basis_test",
        operation_id="operation_test",
        request_hash=quote_creation_request_hash(i),
        opportunity_id=c.opportunity_id,
        cost_sheet_id=i.cost_sheet_id,
        context_hash=c.context_hash,
        sheet_hash=i.expected_sheet_hash,
        basis_hash="b" * 64,
        policy_id=policy.policy_id,
        quantity=c.quantity,
        specification=c.specification,
        unit=c.unit,
        destination=c.destination,
        need_facts=c.need_facts,
        scope_confirmation=scope,
        policy=policy,
        coverage=coverage,
        calculation=calculation,
        price_evidence=(price,),
        pricing_options=q.QuotePricingOptions(
            mode="manual",
            unit_price=i.unit_price,
            rounding=i.rounding,
            quote_fx=None,
            algorithm_version="costing-v1",
        ),
        cost_fx_rates=(),
        quote_fx=None,
        valid_until=i.valid_until,
        frozen_at=NOW,
    )
    return i, basis, c, NOW


def test_build_hash_is_immutable_and_omits_only_created_at():
    assert hasattr(public, "build_quote_content"), "缺少不可变内容工厂"
    i, b, c, now = basis_case()
    content = public.build_quote_content(
        "quote_test", 1, i, b, c, created_at=now, replaced_quote_version=None
    )
    other = public.build_quote_content(
        "quote_test",
        1,
        i,
        b,
        c,
        created_at=now + timedelta(seconds=1),
        replaced_quote_version=None,
    )
    assert content.content_hash == other.content_hash
    assert content.lines[0].description == "hinges"
    assert "Packaging: carton" in content.lines[0].specification
    b.policy.cost_groups["product_purchase"] = "fixed"
    assert content.basis.policy.cost_groups["product_purchase"] == "goods"
    assert public.quote_content_hash(content) == content.content_hash
    tampered = content.model_copy(update={"owner_id": "emp_other"})
    assert public.quote_content_hash(tampered) != content.content_hash


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"quantity": 499}, "basis_mismatch"),
        ({"unit": "kg"}, "basis_mismatch"),
        ({"destination": "UK"}, "basis_mismatch"),
        ({"context_hash": "f" * 64}, "context_changed"),
        ({"valid_until": intent().valid_until + timedelta(days=1)}, "scope_stale"),
    ],
)
def test_second_gate_rejects_changed_basis(changes, code):
    assert hasattr(public, "validate_quote_basis"), "缺少报价第二道门"
    from domains.quotations.errors import QuotationError

    i, b, c, now = basis_case()
    with pytest.raises(QuotationError) as e:
        public.validate_quote_basis(i, b.model_copy(update=changes), c, now=now)
    assert e.value.code == code


def test_second_gate_allows_low_margin_and_supplier_free_text():
    assert hasattr(public, "validate_quote_basis"), "缺少报价第二道门"
    i, b, c, now = basis_case()
    assert public.validate_quote_basis(i, b, c, now=now) is None


@pytest.mark.parametrize(
    "variant",
    [
        "policy_source_missing",
        "policy_wrong_source",
        "policy_unconfirmed",
        "coverage_unconfirmed",
    ],
)
def test_second_gate_rechecks_policy_and_coverage_confirmation(variant):
    from domains.quotations.errors import QuotationError

    i, b, c, now = basis_case()
    if variant == "policy_source_missing":
        b = b.model_copy(
            update={"policy": b.policy.model_copy(update={"source": None})}
        )
    elif variant == "policy_wrong_source":
        b = b.model_copy(
            update={"policy": b.policy.model_copy(update={"source_ref": "other"})}
        )
    else:
        field = "policy" if variant == "policy_unconfirmed" else "coverage"
        b = b.model_copy(
            update={
                field: getattr(b, field).model_copy(update={"field_provenance": {}})
            }
        )
    with pytest.raises(QuotationError):
        public.validate_quote_basis(i, b, c, now=now)


@pytest.mark.parametrize(
    "change",
    [{"basis": "indicative"}, {"unit": "kg"}, {"quantity_max": 400}, {"moq": 501}],
)
def test_second_gate_requires_quoted_quantity_unit(change):
    assert hasattr(public, "validate_quote_basis"), "缺少报价第二道门"
    from domains.quotations.errors import QuotationError

    i, b, c, now = basis_case()
    b = b.model_copy(
        update={"price_evidence": (b.price_evidence[0].model_copy(update=change),)}
    )
    with pytest.raises(QuotationError):
        public.validate_quote_basis(i, b, c, now=now)


def frozen_fixture():
    """将受控领域值组成完整上游DTO；不冒充真实持久冻结。"""
    from domains.costing.schemas import FrozenCostBasis

    _i, b, _c, _now = basis_case()
    raw = json.loads(b.model_dump_json())
    for price in raw["price_evidence"]:
        price.pop("tenant_id")
        price["currency"] = price["amount"]["currency"]
        price["amount"] = price["amount"]["amount"]
    return FrozenCostBasis.model_validate_json(json.dumps(raw))


def test_basis_adapter_copies_complete_upstream_and_cost_fx():
    assert importlib.util.find_spec("workflows.quote_approval.basis_adapter"), (
        "缺少完整冻结适配器"
    )
    from shared.schemas.money import FxRate
    from workflows.quote_approval.basis_adapter import to_quote_basis

    frozen = frozen_fixture()
    fx = FxRate("EUR", "USD", Decimal("1.125"), frozen.frozen_at, "cost fx source")
    frozen = frozen.model_copy(update={"cost_fx_rates": (fx,)})
    mapped = to_quote_basis(frozen)
    assert set(type(mapped).model_fields) == set(type(frozen).model_fields)
    for name in type(frozen).model_fields:
        if name != "price_evidence":
            left = json.loads(mapped.model_dump_json())[name]
            right = json.loads(frozen.model_dump_json())[name]
            assert left == right, name
    assert mapped.price_evidence[0].amount == Money(Decimal("1.25"), "USD")
    assert mapped.price_evidence[0].specification == "Supplier free text"
    assert mapped.cost_fx_rates == (fx,)
    frozen.policy.cost_groups["product_purchase"] = "fixed"
    assert mapped.policy.cost_groups["product_purchase"] == "goods"


def expense_case():
    i, b, c, now = basis_case()
    supplier = b.price_evidence[0]
    price = q.QuoteExpenseEvidence(
        kind="confirmed_expense",
        tenant_id=c.tenant_id,
        opportunity_id=c.opportunity_id,
        evidence_id="expense_test",
        evidence_hash="d" * 64,
        source_ref=supplier.source_ref,
        locator="line:2",
        amount=Money(Decimal("25.00"), "USD"),
        item_type="domestic_freight",
        allocation_scope="order:one",
        is_per_unit=False,
        quantity=c.quantity,
        basis="actual",
        observed_at=now,
        valid_until=None,
        source=supplier.source.model_copy(update={"locator": "line:2"}),
        field_provenance={
            name: supplier.field_provenance["basis"] for name in EXPENSE_FIELDS
        },
        confirmed_by=supplier.confirmed_by,
        confirmed_at=supplier.confirmed_at,
    )
    coverage = b.coverage.model_copy(
        update={
            "decisions": tuple(
                q.QuoteCoverageDecision(
                    item_type="domestic_freight",
                    applicable=True,
                    reason="actual",
                    item_bindings=(
                        q.QuoteCostItemBinding(
                            item_sequence=2,
                            evidence_id=price.evidence_id,
                            source_line_ref=price.locator,
                            allocation_scope=price.allocation_scope,
                        ),
                    ),
                )
                if decision.item_type == "domestic_freight"
                else decision
                for decision in b.coverage.decisions
            )
        }
    )
    coverage = coverage.model_copy(
        update={
            "field_provenance": coverage_provenance(
                coverage.decisions, supplier.field_provenance["basis"]
            )
        }
    )
    scope = b.scope_confirmation.model_copy(
        update={
            "evidence_bindings": b.scope_confirmation.evidence_bindings
            + (
                q.QuoteScopeEvidenceBinding(
                    evidence_id=price.evidence_id,
                    evidence_hash=price.evidence_hash,
                    applicability_note="actual expense",
                ),
            )
        }
    )
    return (
        i,
        b.model_copy(
            update={
                "price_evidence": (supplier, price),
                "coverage": coverage,
                "scope_confirmation": scope,
            }
        ),
        c,
        now,
    )


@pytest.mark.parametrize(
    "variant", [None, "allocation", "quantity", "expiry", "tenant", "unconfirmed"]
)
def test_actual_expense_retains_own_basis_and_requires_exact_applicability(variant):
    from domains.quotations.errors import QuotationError

    i, b, c, now = expense_case()
    changes = {
        "allocation": {"allocation_scope": "other"},
        "quantity": {"quantity": 1},
        "expiry": {"valid_until": i.valid_until - timedelta(seconds=1)},
        "tenant": {"tenant_id": "other"},
        "unconfirmed": {"field_provenance": {}},
    }
    if variant is None:
        assert public.validate_quote_basis(i, b, c, now=now) is None
        assert (
            b.price_evidence[1].basis == "actual"
            and not b.price_evidence[1].is_per_unit
        )
    else:
        price = b.price_evidence[1].model_copy(update=changes[variant])
        with pytest.raises(QuotationError):
            public.validate_quote_basis(
                i,
                b.model_copy(update={"price_evidence": (b.price_evidence[0], price)}),
                c,
                now=now,
            )


def quote_fx_case():
    """有效的独立报价FX样例，只为完整确认门禁提供真实形状。"""
    from shared.schemas.money import FxRate
    from shared.schemas.quote_creation import quote_creation_request_hash

    i, b, c, now = basis_case()
    supplier = b.price_evidence[0]
    i = i.model_copy(update={"quote_fx_ref": "quote_fx"})
    fx = q.QuoteFxSnapshot(
        fx_id="quote_fx",
        content_hash="a" * 64,
        base_currency="EUR",
        quote_currency="USD",
        rate=Decimal("1.125"),
        observed_at=now,
        source_ref=supplier.source_ref,
        source=supplier.source,
        confirmed_by=supplier.confirmed_by,
        confirmed_at=now,
        field_provenance={
            name: supplier.field_provenance["basis"] for name in FX_FIELDS
        },
    )
    return (
        i,
        b.model_copy(
            update={
                "request_hash": quote_creation_request_hash(i),
                "quote_fx": fx,
                "pricing_options": b.pricing_options.model_copy(
                    update={
                        "quote_fx": FxRate("EUR", "USD", fx.rate, now, fx.source_ref)
                    }
                ),
                "calculation": b.calculation.model_copy(
                    update={
                        "base_currency": "EUR",
                        "effective_unit_revenue": Money(Decimal("2.00"), "EUR"),
                    }
                ),
            }
        ),
        c,
        now,
    )


MISSING_CONFIRMATION_FIELDS = (
    [("supplier", name) for name in SUPPLIER_FIELDS]
    + [("expense", name) for name in EXPENSE_FIELDS]
    + [("fx", name) for name in FX_FIELDS]
    + [
        ("policy", name)
        for name in (*POLICY_FIELDS, *(f"cost_groups.{kind}" for kind in COST_TYPES))
    ]
    + [
        ("coverage", name)
        for name in ("cost_sheet_id", "expected_sheet_hash", "acquisition_mode")
    ]
    + [
        ("coverage", f"decisions.{index}.{name}")
        for index in range(22)
        for name in ("item_type", "applicable", "reason")
    ]
    + [("coverage", f"decisions.{index}.item_bindings") for index in range(1, 22)]
    + [
        ("coverage", f"decisions.0.item_bindings.0.{name}")
        for name in (
            "item_sequence",
            "evidence_id",
            "source_line_ref",
            "allocation_scope",
        )
    ]
)


@pytest.mark.parametrize("kind,missing", MISSING_CONFIRMATION_FIELDS)
def test_second_gate_rejects_nonempty_confirmation_missing_any_required_leaf(
    kind, missing
):
    """少查任一确认键会让对应删除样例被错误接受；预期不来自生产字段生成器。"""
    from domains.quotations.errors import QuotationError

    i, b, c, now = (
        expense_case()
        if kind == "expense"
        else quote_fx_case()
        if kind == "fx"
        else basis_case()
    )
    assert public.validate_quote_basis(i, b, c, now=now) is None
    value = (
        b.price_evidence[0]
        if kind == "supplier"
        else b.price_evidence[1]
        if kind == "expense"
        else b.quote_fx
        if kind == "fx"
        else getattr(b, kind)
    )
    incomplete = {key: p for key, p in value.field_provenance.items() if key != missing}
    assert incomplete and missing in value.field_provenance
    changed = value.model_copy(update={"field_provenance": incomplete})
    if kind in {"supplier", "expense"}:
        b = b.model_copy(
            update={
                "price_evidence": tuple(
                    changed if p.evidence_id == value.evidence_id else p
                    for p in b.price_evidence
                )
            }
        )
    else:
        b = b.model_copy(update={"quote_fx" if kind == "fx" else kind: changed})
    with pytest.raises(QuotationError) as error:
        public.validate_quote_basis(i, b, c, now=now)
    assert error.value.code == "evidence_invalid"


@pytest.mark.parametrize("kind", ["supplier", "expense"])
def test_second_gate_uses_original_amount_currency_confirmation_keys_not_money_paths(
    kind,
):
    from domains.quotations.errors import QuotationError

    i, b, c, now = expense_case() if kind == "expense" else basis_case()
    position = 1 if kind == "expense" else 0
    value = b.price_evidence[position]
    changed = {
        key: p
        for key, p in value.field_provenance.items()
        if key not in {"amount", "currency"}
    }
    changed.update(
        {
            "amount.amount": value.field_provenance["amount"],
            "amount.currency": value.field_provenance["currency"],
        }
    )
    price = value.model_copy(update={"field_provenance": changed})
    b = b.model_copy(
        update={
            "price_evidence": tuple(
                price if n == position else p for n, p in enumerate(b.price_evidence)
            )
        }
    )
    with pytest.raises(QuotationError) as error:
        public.validate_quote_basis(i, b, c, now=now)
    assert error.value.code == "evidence_invalid"


@pytest.mark.parametrize(
    "missing", ["item_sequence", "evidence_id", "source_line_ref", "allocation_scope"]
)
def test_second_gate_checks_every_binding_not_only_first(missing):
    from domains.quotations.errors import QuotationError

    i, b, c, now = basis_case()
    original = b.coverage.decisions[0]
    second = original.item_bindings[0].model_copy(
        update={"item_sequence": 2, "allocation_scope": "order:two"}
    )
    decisions = (
        original.model_copy(
            update={"item_bindings": original.item_bindings + (second,)}
        ),
    ) + b.coverage.decisions[1:]
    provenance = coverage_provenance(
        decisions, b.price_evidence[0].field_provenance["basis"]
    )
    coverage = b.coverage.model_copy(
        update={"decisions": decisions, "field_provenance": provenance}
    )
    b = b.model_copy(update={"coverage": coverage})
    assert public.validate_quote_basis(i, b, c, now=now) is None
    incomplete = {
        key: p
        for key, p in provenance.items()
        if key != f"decisions.0.item_bindings.1.{missing}"
    }
    with pytest.raises(QuotationError) as error:
        public.validate_quote_basis(
            i,
            b.model_copy(
                update={
                    "coverage": coverage.model_copy(
                        update={"field_provenance": incomplete}
                    )
                }
            ),
            c,
            now=now,
        )
    assert error.value.code == "evidence_invalid"


def test_second_gate_rejects_supplier_with_only_basis_confirmation():
    from domains.quotations.errors import QuotationError

    i, b, c, now = basis_case()
    price = b.price_evidence[0]
    price = price.model_copy(
        update={"field_provenance": {"basis": price.field_provenance["basis"]}}
    )
    with pytest.raises(QuotationError) as error:
        public.validate_quote_basis(
            i, b.model_copy(update={"price_evidence": (price,)}), c, now=now
        )
    assert error.value.code == "evidence_invalid"


def test_adapter_expense_and_independent_quote_fx_copy_every_field():
    from domains.costing.schemas import FrozenCostBasis
    from workflows.quote_approval.basis_adapter import to_quote_basis

    frozen = frozen_fixture()
    _, expense_basis, _, now = expense_case()
    raw = json.loads(frozen.model_dump_json())
    expense = json.loads(expense_basis.price_evidence[1].model_dump_json())
    expense.pop("tenant_id")
    expense["amount"], expense["currency"] = (
        expense["amount"]["amount"],
        expense["amount"]["currency"],
    )
    raw["price_evidence"].append(expense)
    supplier = expense_basis.price_evidence[0]
    fx = q.QuoteFxSnapshot(
        fx_id="quote_fx",
        content_hash="a" * 64,
        base_currency="EUR",
        quote_currency="USD",
        source_ref=supplier.source_ref,
        rate=Decimal("1.125"),
        observed_at=now,
        source=supplier.source,
        field_provenance={
            name: supplier.field_provenance["basis"] for name in FX_FIELDS
        },
        confirmed_by=supplier.confirmed_by,
        confirmed_at=now,
    )
    raw["quote_fx"] = json.loads(fx.model_dump_json())
    upstream = FrozenCostBasis.model_validate_json(json.dumps(raw))
    mapped = to_quote_basis(upstream)
    assert mapped.price_evidence[1] == expense_basis.price_evidence[1]
    assert mapped.quote_fx == fx
    assert set(type(mapped.price_evidence[1]).model_fields) - {"tenant_id"} == set(
        type(upstream.price_evidence[1]).model_fields
    ) - {"currency"}
    assert set(type(mapped.quote_fx).model_fields) == set(
        type(upstream.quote_fx).model_fields
    )


def test_second_gate_rejects_quote_fx_currency_pair_different_from_calculation():
    from domains.quotations.errors import QuotationError
    from shared.schemas.money import FxRate
    from shared.schemas.quote_creation import quote_creation_request_hash

    i, b, c, now = basis_case()
    supplier = b.price_evidence[0]
    i = i.model_copy(update={"quote_fx_ref": "fx_test"})
    fx = q.QuoteFxSnapshot(
        fx_id="fx_test",
        content_hash="a" * 64,
        base_currency="EUR",
        quote_currency="GBP",
        source_ref=supplier.source_ref,
        rate=Decimal("1.1"),
        observed_at=now,
        source=supplier.source,
        field_provenance={
            name: supplier.field_provenance["basis"] for name in FX_FIELDS
        },
        confirmed_by=supplier.confirmed_by,
        confirmed_at=now,
    )
    b = b.model_copy(
        update={
            "request_hash": quote_creation_request_hash(i),
            "quote_fx": fx,
            "pricing_options": b.pricing_options.model_copy(
                update={
                    "quote_fx": FxRate(
                        "EUR", "GBP", Decimal("1.1"), now, supplier.source_ref
                    )
                }
            ),
        }
    )
    with pytest.raises(QuotationError) as error:
        public.validate_quote_basis(i, b, c, now=now)
    assert error.value.code == "basis_mismatch"


def detail_fixture():
    i, b, c, now = basis_case()
    return q.QuoteDetailView(
        content=public.build_quote_content(
            "quote_test", 1, i, b, c, created_at=now, replaced_quote_version=None
        ),
        state=q.QuoteState.APPROVED,
    )


def test_customer_projection_exact_whitelist_and_deterministic_amounts():
    assert hasattr(public, "project_customer"), "缺少客户白名单投影"
    view = public.project_customer(detail_fixture())
    assert view.unit_price_display == "2.30"
    assert view.total_display == "1150.00"
    assert view.quantity_display == "500"
    assert view.approved_terms == ("Payment terms: prepaid.",)
    assert set(view.model_dump()) == {
        "quote_id",
        "version",
        "issuer_name",
        "issuer_address",
        "issuer_contact",
        "account_name",
        "description",
        "specification",
        "unit",
        "quantity_display",
        "unit_price_display",
        "total_display",
        "currency",
        "valid_until_display",
        "approved_terms",
    }
    assert view.valid_until_display == "2026-09-01T00:00:00+00:00"
    raw = view.model_dump_json()
    for forbidden in (
        "cost_sheet",
        "basis",
        "supplier_ref",
        "profit",
        "source_quote",
        "artifact_test",
    ):
        assert forbidden not in raw
    assert public.validate_customer_projection(view, detail_fixture()) is None


@pytest.mark.parametrize(
    "name",
    [
        "quote_id",
        "version",
        "issuer_name",
        "issuer_address",
        "issuer_contact",
        "account_name",
        "description",
        "specification",
        "unit",
        "quantity_display",
        "unit_price_display",
        "total_display",
        "currency",
        "valid_until_display",
        "approved_terms",
    ],
)
def test_customer_projection_rejects_every_changed_field(name):
    assert hasattr(public, "validate_customer_projection"), "缺少客户内容绑定"
    from domains.quotations.errors import QuotationError

    detail = detail_fixture()
    view = public.project_customer(detail)
    replacement = (
        2
        if name == "version"
        else ("extra",)
        if name == "approved_terms"
        else "arbitrary text"
    )
    with pytest.raises(QuotationError) as e:
        public.validate_customer_projection(
            view.model_copy(update={name: replacement}), detail
        )
    assert e.value.code == "basis_mismatch"


def test_legacy_projection_does_not_guess_names_from_ids():
    assert hasattr(public, "to_legacy_quote_view"), "缺少旧视图纯投影"
    legacy = public.to_legacy_quote_view(detail_fixture())
    assert isinstance(legacy, q.QuoteView)
    assert legacy.prepared_by_name is None and legacy.approved_by_name is None
    assert legacy.lines[0].quantity == 500
