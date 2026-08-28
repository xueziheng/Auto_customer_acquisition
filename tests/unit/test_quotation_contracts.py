"""不可变报价新契约与旧模型保持独立；所有来源都是受控样例。"""

from datetime import timedelta
from decimal import Decimal
import importlib
import json

import pytest
from pydantic import ValidationError

from domains.quotations import schemas as q
from domains.quotations import service as public
from shared.schemas.money import Money
from shared.schemas.quote_creation import QuoteRoundingInput
from tests.unit.test_quote_context_contracts import business_context, intent


def contracts():
    assert hasattr(q, "QuoteContentLine"), "缺少不可变报价内容契约"
    return q


def line(**changes):
    return contracts().QuoteContentLine(**(dict(line_number=1, description="Part",
        specification="Material: Steel", unit="piece", quantity=3,
        unit_price=Money(Decimal("1.235"), "USD"),
        line_total=Money(Decimal("3.71"), "USD"),
        rounding=QuoteRoundingInput(unit_places=3, total_places=2, strategy="ROUND_HALF_UP")) | changes))


def test_new_line_rounds_without_weakening_legacy_line():
    value = line()
    assert value.line_total.amount == Decimal("3.71")
    from shared.schemas.quote_document import CustomerQuoteView
    assert q.CustomerQuoteView is CustomerQuoteView
    old = importlib.import_module("domains.quotations.models")
    from shared.errors import ValidationError as DomainError
    with pytest.raises(DomainError):
        old.QuoteLine(1, "Part", 3, value.unit_price, value.line_total, "price")


@pytest.mark.parametrize("changes", [dict(quantity=True), dict(line_number=2),
    dict(line_total=Money(Decimal("3.70"), "USD")), dict(unit=" piece"), dict(approved=True)])
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


@pytest.mark.parametrize("changes", [dict(expected_quote_version=True),
    dict(replaces_quote_id="q_old"), dict(valid_until=intent().valid_until.replace(tzinfo=None)),
    dict(prepared_by="emp_other"), dict(unit_price=Money(Decimal("1E-13"), "USD")),
    dict(unit_price=Money(Decimal("1E+100000"), "USD"))])
def test_draft_is_strict_and_has_no_trusted_client_fields(changes):
    contracts()
    from domains.quotations.errors import QuotationError
    with pytest.raises((ValidationError, QuotationError)):
        draft(**changes)


def test_specification_uses_all_confirmed_dimensions_in_fixed_order():
    contracts()
    spec = q.QuoteSpecificationFacts(product_category="Hinges", application="Doors",
        material="Steel", size_spec="50mm", packaging="Cartons", certification_required="CE")
    assert public.format_quote_specification(spec) == (
        "Product category: Hinges\nApplication: Doors\nMaterial: Steel\nSize: 50mm\n"
        "Packaging: Cartons\nCertification required: CE")


def test_new_states_do_not_open_approval_bypass():
    m = importlib.import_module("domains.quotations.models")
    for state in (m.QuoteState.DRAFT, m.QuoteState.PENDING_APPROVAL, m.QuoteState.APPROVED):
        assert m.QuoteState.EXPIRED in m.ALLOWED_TRANSITIONS[state]
        assert m.QuoteState.SUPERSEDED in m.ALLOWED_TRANSITIONS[state]
    assert m.QuoteState.SENT not in m.ALLOWED_TRANSITIONS[m.QuoteState.DRAFT]


def basis_case():
    """完整受控快照用于纯规则；真实冻结真实性在集成测试验证。"""
    from dataclasses import replace
    from shared.schemas.provenance import SourceType
    from shared.schemas.quote_creation import quote_creation_request_hash, quote_terms_hash
    from tests.unit.test_need_units import NOW, field
    c = business_context()
    i = intent(expected_context_hash=c.context_hash)
    p = replace(field("raw evidence").provenance, source_type=SourceType.UPLOAD,
        source_id="artifact_test")
    source = q.QuoteEvidenceSource(tenant_id=c.tenant_id, source_ref="artifact_test", artifact_id="artifact_test",
        content_hash="d"*64, locator="line:1", observed_at=NOW, source_type="upload", source_url=None)
    common = dict(source=source, confirmed_by=i.prepared_by, confirmed_at=NOW)
    supplier_values = dict(kind="supplier_price", tenant_id=c.tenant_id, opportunity_id=c.opportunity_id,
        evidence_id="ev_test", evidence_hash="e"*64, source_ref="artifact_test", locator="line:1",
        amount=Money(Decimal("1.25"), "USD"), need_id=c.need_id, supplier_ref="supplier", specification="Supplier free text",
        unit=c.unit, destination=c.destination, basis="quoted", quantity_min=100, quantity_max=1000,
        moq=100, quoted_at=NOW, valid_until=i.valid_until + timedelta(days=1))
    provenance_keys = set(supplier_values) - {"tenant_id", "evidence_id", "evidence_hash"}
    provenance_keys.add("currency")
    price = q.QuoteSupplierEvidence(**supplier_values, **common, field_provenance={k:p for k in provenance_keys})
    policy = q.QuotePolicySnapshot(policy_id="policy_test", content_hash="d"*64, category="hinges",
        minimum_margin_rate=Decimal("0.1"), target_margin_rate=Decimal("0.2"), cost_groups={"product_purchase":"goods"},
        effective_from=NOW, source_ref="artifact_test", source=source, confirmed_by=i.prepared_by, confirmed_at=NOW,
        field_provenance={k:p for k in ("category","minimum_margin_rate","target_margin_rate","cost_groups","effective_from","source_ref")})
    coverage = q.QuoteCoverageSnapshot(expected_sheet_hash=i.expected_sheet_hash,
        decisions=(q.QuoteCoverageDecision(item_type="product_purchase", applicable=True, reason="confirmed",
            item_bindings=(q.QuoteCostItemBinding(item_sequence=1, evidence_id=price.evidence_id,
                source_line_ref="line:1", allocation_scope="order:one"),)),), acquisition_mode="detail",
        coverage_id="a"*64, cost_sheet_id=i.cost_sheet_id, content_hash="a"*64,
        confirmed_by=i.prepared_by, confirmed_at=NOW, field_provenance={"decisions":p})
    scope = q.QuoteScopeConfirmation(tenant_id=c.tenant_id, confirmation_id=i.scope_confirmation_id,
        opportunity_id=c.opportunity_id, need_id=c.need_id, cost_sheet_id=i.cost_sheet_id, sheet_hash=i.expected_sheet_hash,
        coverage_id=coverage.coverage_id, coverage_hash=coverage.content_hash, need_facts=c.need_facts,
        need_facts_hash=c.need_facts_hash, specification=c.specification, specification_hash=c.specification_hash,
        terms=i.terms, terms_hash=quote_terms_hash(i.terms), valid_until=i.valid_until,
        evidence_bindings=(q.QuoteScopeEvidenceBinding(evidence_id=price.evidence_id,evidence_hash=price.evidence_hash,
            applicability_note="confirmed all dimensions"),), content_hash=i.scope_confirmation_hash, provenance=p)
    metrics=q.QuoteProfitMetrics(**{name:Decimal("0.01") for name in q.QuoteProfitMetrics.model_fields})
    calculation=q.QuoteCalculationSnapshot(cost_sheet_id=i.cost_sheet_id,policy_id=policy.policy_id,inputs_hash="a"*64,
        context_hash=c.context_hash, version_number=1, computed_at=NOW, base_currency="USD",quote_currency="USD",
        metrics=metrics,effective_unit_revenue=i.unit_price, displayed_unit_price=i.unit_price,
        displayed_total=Money(Decimal("1150.00"), "USD"))
    basis=q.QuoteBasis(tenant_id=c.tenant_id,basis_id="basis_test", operation_id="operation_test",
        request_hash=quote_creation_request_hash(i),opportunity_id=c.opportunity_id,cost_sheet_id=i.cost_sheet_id,
        context_hash=c.context_hash,sheet_hash=i.expected_sheet_hash,basis_hash="b"*64,policy_id=policy.policy_id,
        quantity=c.quantity,specification=c.specification,unit=c.unit,destination=c.destination,need_facts=c.need_facts,
        scope_confirmation=scope,policy=policy,coverage=coverage,calculation=calculation,price_evidence=(price,),
        pricing_options=q.QuotePricingOptions(mode="manual",unit_price=i.unit_price,rounding=i.rounding,quote_fx=None,
            algorithm_version="costing-v1"),cost_fx_rates=(),quote_fx=None,valid_until=i.valid_until,frozen_at=NOW)
    return i,basis,c,NOW


def test_build_hash_is_immutable_and_omits_only_created_at():
    assert hasattr(public,"build_quote_content"), "缺少不可变内容工厂"
    i,b,c,now=basis_case()
    content=public.build_quote_content("quote_test",1,i,b,c,created_at=now,replaced_quote_version=None)
    other=public.build_quote_content("quote_test",1,i,b,c,created_at=now+timedelta(seconds=1),replaced_quote_version=None)
    assert content.content_hash == other.content_hash
    assert content.lines[0].description == "hinges"
    assert "Packaging: carton" in content.lines[0].specification
    b.policy.cost_groups["product_purchase"]="fixed"
    assert content.basis.policy.cost_groups["product_purchase"] == "goods"
    assert public.quote_content_hash(content) == content.content_hash
    tampered=content.model_copy(update={"owner_id":"emp_other"})
    assert public.quote_content_hash(tampered) != content.content_hash


@pytest.mark.parametrize("changes,code", [(dict(quantity=499),"basis_mismatch"),
    (dict(unit="kg"),"basis_mismatch"),(dict(destination="UK"),"basis_mismatch"),
    (dict(context_hash="f"*64),"context_changed"),(dict(valid_until=intent().valid_until+timedelta(days=1)),"scope_stale")])
def test_second_gate_rejects_changed_basis(changes,code):
    assert hasattr(public,"validate_quote_basis"), "缺少报价第二道门"
    from domains.quotations.errors import QuotationError
    i,b,c,now=basis_case()
    with pytest.raises(QuotationError) as e:
        public.validate_quote_basis(i,b.model_copy(update=changes),c,now=now)
    assert e.value.code == code


def test_second_gate_allows_low_margin_and_supplier_free_text():
    assert hasattr(public,"validate_quote_basis"), "缺少报价第二道门"
    i,b,c,now=basis_case()
    assert public.validate_quote_basis(i,b,c,now=now) is None


@pytest.mark.parametrize("change", [dict(basis="indicative"),dict(unit="kg"),dict(quantity_max=400),dict(moq=501)])
def test_second_gate_requires_quoted_quantity_unit(change):
    assert hasattr(public,"validate_quote_basis"), "缺少报价第二道门"
    from domains.quotations.errors import QuotationError
    i,b,c,now=basis_case()
    b=b.model_copy(update={"price_evidence":(b.price_evidence[0].model_copy(update=change),)})
    with pytest.raises(QuotationError):
        public.validate_quote_basis(i,b,c,now=now)


def frozen_fixture():
    """将受控领域值组成完整上游DTO；不冒充真实持久冻结。"""
    from domains.costing.schemas import FrozenCostBasis
    from domains.costing.service import cost_item_type_values
    i,b,c,now=basis_case()
    raw=json.loads(b.model_dump_json())
    raw["policy"]["cost_groups"]={k:"goods" if k=="product_purchase" else "fixed" for k in cost_item_type_values()}
    raw["coverage"]["decisions"] += [dict(item_type=k,applicable=False,reason="not applicable",item_bindings=[])
        for k in cost_item_type_values() if k!="product_purchase"]
    for price in raw["price_evidence"]:
        price.pop("tenant_id")
        price["currency"]=price["amount"]["currency"]
        price["amount"]=price["amount"]["amount"]
    return FrozenCostBasis.model_validate_json(json.dumps(raw))


def test_basis_adapter_copies_complete_upstream_and_cost_fx():
    assert importlib.util.find_spec("workflows.quote_approval.basis_adapter"), "缺少完整冻结适配器"
    from shared.schemas.money import FxRate
    from workflows.quote_approval.basis_adapter import to_quote_basis
    frozen=frozen_fixture()
    fx=FxRate("EUR","USD",Decimal("1.125"),frozen.frozen_at,"cost fx source")
    frozen=frozen.model_copy(update={"cost_fx_rates":(fx,)})
    mapped=to_quote_basis(frozen)
    assert set(type(mapped).model_fields)==set(type(frozen).model_fields)
    for name in type(frozen).model_fields:
        if name!="price_evidence":
            left=json.loads(mapped.model_dump_json())[name]
            right=json.loads(frozen.model_dump_json())[name]
            assert left==right, name
    assert mapped.price_evidence[0].amount==Money(Decimal("1.25"),"USD")
    assert mapped.price_evidence[0].specification=="Supplier free text"
    assert mapped.cost_fx_rates==(fx,)
    frozen.policy.cost_groups["product_purchase"]="fixed"
    assert mapped.policy.cost_groups["product_purchase"]=="goods"
