"""单轮审批载荷与决定身份；受控完整T4事实不代表真实商业依据。"""

import importlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domains.quotations import schemas as q
from domains.quotations import service as public
from domains.quotations.errors import QuoteApprovalError
from shared.schemas.identifiers import ApprovalId, EmployeeId, QuoteId, RunId
from shared.schemas.money import FxRate
from shared.schemas.quote_creation import QuoteTerm
from tests.unit.test_quotation_contracts import basis_case

QUOTE = QuoteId("quo_01K00000000000000000000000")
DECIDER = EmployeeId("emp_01K00000000000000000000002")
RUN = RunId("run_01K00000000000000000000000")


def quote_case(*, version=1, cost_fx=(), quote_fx=None, terms=()):
    """从完整已确认字段fixture构造真实内容工厂结果。"""
    intent, basis, context, now = basis_case()
    basis = basis.model_copy(update={"cost_fx_rates": cost_fx, "quote_fx": quote_fx})
    content = public.build_quote_content(
        QUOTE,
        version,
        intent,
        basis,
        context,
        created_at=now,
        replaced_quote_version=None,
    ).model_copy(update={"terms": terms})
    return q.QuoteDetailView(content=content, state=q.QuoteState.DRAFT)


def api():
    assert hasattr(public, "quote_approval_payloads"), "缺少安全审批载荷工厂"
    return public


def approved_facts():
    """完整决定字段，hash不使用展示姓名替代员工ID。"""
    quote = quote_case()
    payloads = api().quote_approval_payloads(quote, None)
    return tuple(
        q.QuoteApprovalFact(
            tenant_id=p.tenant_id,
            approval_id=ApprovalId(f"apr_01K0000000000000000000000{index}"),
            approval_type=p.approval_type,
            change_set_ref=public.quote_change_set_ref(
                QUOTE, p.content_hash, p.approval_type
            ),
            request_hash="a" * 64,
            payload=p,
            created_at=quote.content.created_at,
            expires_at=quote.content.valid_until,
            expires_at_limit=quote.content.valid_until,
            prepared_by=p.prepared_by,
            submitted_owner_id=p.submitted_owner_id,
            proposed_by_run=RUN,
            state="approved",
            decision="approve",
            decided_by=DECIDER,
            decided_at=quote.content.created_at,
            decision_note=None,
            applied_at=None,
            application_error_code=None,
        )
        for index, p in enumerate(payloads)
    )


def test_required_types_are_deterministic_independent_and_preserve_terms():
    terms = tuple(
        QuoteTerm(kind=k, text="Confirmed terms")
        for k in (
            "payment_terms",
            "discount",
            "certification_commitment",
            "delivery_commitment",
            "discount",
        )
    )
    quote = quote_case(terms=terms)
    assert api().required_quote_approvals(quote) == (
        "quote_send",
        "margin_floor_override",
        "discount",
        "delivery_commitment",
        "payment_terms",
        "certification_commitment",
    )
    payloads = public.quote_approval_payloads(quote, None)
    assert all(p.customer.terms == terms for p in payloads)
    assert len(payloads) == 6
    higher = quote.model_copy(
        update={
            "content": quote.content.model_copy(
                update={
                    "basis": quote.content.basis.model_copy(
                        update={
                            "calculation": quote.content.basis.calculation.model_copy(
                                update={
                                    "metrics": quote.content.basis.calculation.metrics.model_copy(
                                        update={"margin_rate": Decimal("0.1")}
                                    )
                                }
                            )
                        }
                    )
                }
            )
        }
    )
    assert "margin_floor_override" not in public.required_quote_approvals(higher)


def test_payload_roundtrip_whitelist_and_actual_cost_fx_order():
    now = basis_case()[3]
    rates = (
        FxRate(
            "EUR", "USD", Decimal("1.123400"), now, "https://private.invalid/secret"
        ),
        FxRate("CNY", "USD", Decimal("0.125"), now - timedelta(hours=1), "raw-source"),
    )
    quote = quote_case(cost_fx=rates)
    previous = quote_case(cost_fx=(rates[1],))
    payload = api().quote_approval_payloads(quote, previous)[0]
    assert [
        (r.source_currency, r.target_currency, r.rate, r.observed_at, r.reference_id)
        for r in payload.calculation.cost_fx_rates
    ] == [
        ("EUR", "USD", Decimal("1.123400"), now, quote.content.basis.cost_sheet_id),
        (
            "CNY",
            "USD",
            Decimal("0.125"),
            now - timedelta(hours=1),
            quote.content.basis.cost_sheet_id,
        ),
    ]
    assert payload.calculation.quote_fx is None
    assert payload.previous.calculation.cost_fx_rates[0].source_currency == "CNY"
    raw = json.loads(payload.model_dump_json())
    assert public.parse_quote_approval_payload(raw) == payload
    encoded = json.dumps(raw)
    for forbidden in (
        "private.invalid",
        "raw-source",
        "source_ref",
        "source_url",
        "locator",
        "field_provenance",
        "scope_confirmation",
        "need_facts",
    ):
        assert forbidden not in encoded
    assert "confirmed_by" not in raw["calculation"]["cost_fx_rates"][0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("rate", Decimal("1.2")),
        ("observed_at", basis_case()[3] + timedelta(seconds=1)),
        ("reference_id", "sheet_other"),
    ],
)
def test_each_fx_fact_changes_payload_hash(field, value):
    now = basis_case()[3]
    quote = quote_case(cost_fx=(FxRate("EUR", "USD", Decimal("1.1"), now, "raw"),))
    payload = api().quote_approval_payloads(quote, None)[0]
    altered = payload.model_copy(
        update={
            "calculation": payload.calculation.model_copy(
                update={
                    "cost_fx_rates": (
                        payload.calculation.cost_fx_rates[0].model_copy(
                            update={field: value}
                        ),
                    )
                }
            )
        }
    )
    assert public.quote_approval_payload_hash(
        payload
    ) != public.quote_approval_payload_hash(altered)


def test_applied_state_does_not_change_decision_facts_hash():
    facts = approved_facts()
    applied = tuple(
        f.model_copy(update={"state": "applied", "applied_at": f.decided_at})
        for f in facts
    )
    assert public.quote_approval_facts_hash(
        applied
    ) == public.quote_approval_facts_hash(facts)
    for key, value in {
        "decision_note": "different",
        "decided_by": "emp_other",
        "decided_at": facts[0].decided_at + timedelta(seconds=1),
        "expires_at_limit": facts[0].expires_at_limit + timedelta(seconds=1),
        "request_hash": "b" * 64,
    }.items():
        changed = (facts[0].model_copy(update={key: value}), *facts[1:])
        assert public.quote_approval_facts_hash(
            changed
        ) != public.quote_approval_facts_hash(facts)
    assert public.quote_approval_facts_hash(
        tuple(reversed(facts))
    ) == public.quote_approval_facts_hash(facts)


@pytest.mark.parametrize(
    "mutation", ["extra", "money_number", "oversized", "schema", "type"]
)
def test_persisted_payload_rejects_invalid_or_oversized_json(mutation):
    payload = api().quote_approval_payloads(quote_case(), None)[0]
    raw = json.loads(payload.model_dump_json())
    if mutation == "extra":
        raw["source_url"] = "https://private.invalid"
    elif mutation == "money_number":
        raw["calculation"]["displayed_total"]["amount"] = 1150
    elif mutation == "oversized":
        raw["customer"]["terms"] = [{"kind": "discount", "text": "x" * 4096}] * 20
    elif mutation == "schema":
        raw["schema_version"] = "quote-approval-v2"
    else:
        raw["approval_type"] = "price_communication"
    with pytest.raises((ValidationError, ValueError, QuoteApprovalError)):
        public.parse_quote_approval_payload(raw)


@pytest.mark.parametrize("prefix", [" quote:", "QUOTE:", "quote:", ""])
def test_partial_or_disguised_namespace_never_falls_back(prefix):
    assert importlib.util.find_spec("domains.approvals.quote_contract"), (
        "缺少严格namespace契约"
    )
    contract = importlib.import_module("domains.approvals.quote_contract")
    value = (
        {"schema_version": "quote-approval-v1"} if not prefix else {"legacy": "email"}
    )
    from domains.approvals.errors import QuoteContractError

    with pytest.raises(QuoteContractError):
        contract.quote_contract_subject(
            tenant_id="tenant",
            approval_id=None,
            approval_type="quote_send",
            change_set_ref=prefix + "invalid" if prefix else None,
            proposed_change=value,
            proposed_by_employee=None,
            owner_employee=None,
        )


def test_unmarked_legacy_quote_send_is_not_new_namespace():
    assert importlib.util.find_spec("domains.approvals.quote_contract"), (
        "缺少严格namespace契约"
    )
    contract = importlib.import_module("domains.approvals.quote_contract")
    assert (
        contract.quote_contract_subject(
            tenant_id="tenant",
            approval_id=None,
            approval_type="quote_send",
            change_set_ref="email:attempt",
            proposed_change={"subject": "legacy"},
            proposed_by_employee=None,
            owner_employee=None,
        )
        is None
    )


@pytest.mark.parametrize(
    "role,own,active,manager_match,can_read,can_decide",
    [
        ("boss", False, True, False, True, True),
        ("boss", True, True, False, True, False),
        ("manager", False, True, True, True, True),
        ("manager", False, True, False, False, False),
        ("manager", True, True, True, True, False),
        ("finance", True, True, False, True, False),
        ("finance", False, True, False, False, False),
        ("boss", False, False, False, False, False),
    ],
)
def test_current_abac_distinguishes_own_read_from_independent_decision(
    role,
    own,
    active,
    manager_match,
    can_read,
    can_decide,
):
    from domains.quotations.errors import QuoteApprovalPermissionError
    from shared.schemas.quote_facts import QuoteEmployeeFact

    payload = api().quote_approval_payloads(quote_case(), None)[0]
    actor_id = payload.prepared_by if own else DECIDER
    subject = q.QuoteApprovalSubject(
        **{
            name: getattr(payload, name)
            for name in q.QuoteApprovalSubject.model_fields
            if name != "approval_id"
        },
        approval_id=None,
    )
    context = q.QuoteApprovalAccessContext(
        tenant_id=payload.tenant_id,
        opportunity_id=payload.opportunity_id,
        prepared_by=payload.prepared_by,
        submitted_owner_id=payload.submitted_owner_id,
        actor=QuoteEmployeeFact(
            tenant_id=payload.tenant_id,
            employee_id=actor_id,
            role=role,
            is_active=active,
            manager_id=None,
            team_id=None,
        ),
        owner=QuoteEmployeeFact(
            tenant_id=payload.tenant_id,
            employee_id=payload.submitted_owner_id,
            role="sales",
            is_active=True,
            manager_id=actor_id if manager_match else None,
            team_id=None,
        ),
    )
    for action, allowed in (
        ("read", can_read),
        ("decide", can_decide),
        ("apply", can_decide),
    ):
        if allowed:
            public.require_quote_approval_access(subject, context, action=action)
        else:
            with pytest.raises(QuoteApprovalPermissionError):
                public.require_quote_approval_access(subject, context, action=action)


def test_quote_fx_uses_its_real_id_and_preserves_no_cost_fx():
    from tests.unit.test_quotation_contracts import FX_FIELDS

    _, basis, _, now = basis_case()
    confirmation = basis.price_evidence[0]
    provenance = next(iter(confirmation.field_provenance.values()))
    fx = q.QuoteFxSnapshot(
        fx_id="fx_quote",
        content_hash="f" * 64,
        base_currency="USD",
        quote_currency="EUR",
        source_ref=confirmation.source_ref,
        rate=Decimal("0.91"),
        observed_at=now,
        source=confirmation.source,
        confirmed_by=confirmation.confirmed_by,
        confirmed_at=confirmation.confirmed_at,
        field_provenance={k: provenance for k in FX_FIELDS},
    )
    payload = api().quote_approval_payloads(quote_case(quote_fx=fx), None)[0]
    assert payload.calculation.cost_fx_rates == ()
    assert payload.calculation.quote_fx.reference_id == "fx_quote"
    assert payload.calculation.quote_fx.rate == Decimal("0.91")
    assert payload.calculation.quote_fx.source_currency == "USD"
    assert payload.calculation.quote_fx.target_currency == "EUR"


def test_missing_frozen_cost_fx_is_not_silently_defaulted():
    quote = quote_case()
    raw = json.loads(quote.content.basis.model_dump_json())
    del raw["cost_fx_rates"]
    with pytest.raises(ValidationError):
        q.QuoteBasis.model_validate_json(json.dumps(raw))
