"""报价共享事实与完整意图：hash兼容及不遗漏请求字段。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from domains.demand import schemas as demand
from domains.demand import service as demand_service
from shared.schemas.money import Money
from tests.unit.test_need_units import ACTOR, NEED, TENANT, bound_facts, field


def creation_module():
    """缺失公共能力按行为断言失败，不以环境导入错误作为RED。"""
    from domains.quotations import service

    assert hasattr(service, "quote_creation_request_hash"), "缺少完整创建意图hash"
    return importlib.import_module("shared.schemas.quote_creation")


def intent(**changes: object):
    """受控完整意图，不设置生产业务默认值。"""
    module = creation_module()
    values = {
        "tenant_id": TENANT,
        "prepared_by": ACTOR,
        "opportunity_id": "opp_test",
        "cost_sheet_id": "cs_test",
        "expected_context_hash": "a" * 64,
        "expected_sheet_hash": "b" * 64,
        "valid_until": datetime(2026, 9, 1, tzinfo=UTC),
        "unit_price": Money(Decimal("2.30"), "USD"),
        "rounding": module.QuoteRoundingInput(
            unit_places=2, total_places=2, strategy="ROUND_HALF_UP"
        ),
        "quote_fx_ref": None,
        "terms": (
            module.QuoteTerm(kind="payment_terms", text="Payment terms: prepaid."),
        ),
        "replaces_quote_id": None,
        "expected_quote_version": None,
        "scope_confirmation_id": "scope_test",
        "scope_confirmation_hash": "c" * 64,
    }
    return module.QuoteCreationIntent(**(values | changes))


def test_need_fact_public_type_identity_and_golden_hash() -> None:
    assert importlib.util.find_spec("shared.schemas.quote_facts"), "缺少共享报价事实"
    shared = importlib.import_module("shared.schemas.quote_facts")
    assert demand.NeedQuoteFacts is shared.NeedQuoteFacts
    assert demand_service.quantity_fact_hash(TENANT, NEED, field(500)) == (
        "87d8a18e5997cdb0d09cdc597c602ede5ddc4b989f5c19e85dc763b048355d82"
    )
    assert demand_service.need_quote_facts_hash(
        bound_facts(target_price=field(Money(Decimal("1.2300"), "USD")))
    ) == ("c20cc2aa69627c1d0ac2a1ee2d50d0aa653832faec1646db7f84ae5bebe0994b")


def test_intent_hash_normalizes_decimal_without_context_rounding() -> None:
    module = creation_module()
    for value in ("2.3", "2.30", "2.3000", "23E-1"):
        assert module.quote_creation_request_hash(
            intent(unit_price=Money(Decimal(value), "USD"))
        ) == (module.quote_creation_request_hash(intent()))
    high = intent(
        unit_price=Money(Decimal("1.23456789012345678901234567890123456789"), "USD")
    )
    with localcontext() as ctx:
        ctx.prec = 5
        digest = module.quote_creation_request_hash(high)
    assert digest == module.quote_creation_request_hash(high)
    assert digest != module.quote_creation_request_hash(
        intent(unit_price=Money(Decimal("1.2346"), "USD"))
    )


def test_intent_hash_covers_each_field_and_preserves_order_duplicates() -> None:
    module = creation_module()
    original = intent()
    digest = module.quote_creation_request_hash(original)
    term = module.QuoteTerm(kind="delivery_commitment", text="Delivery within 30 days.")
    changes = [
        {"tenant_id": "tenant_other"},
        {"prepared_by": "emp_other"},
        {"opportunity_id": "opp_other"},
        {"cost_sheet_id": "cs_other"},
        {"expected_context_hash": "d" * 64},
        {"expected_sheet_hash": "e" * 64},
        {"valid_until": original.valid_until + timedelta(seconds=1)},
        {"unit_price": Money(Decimal("2.31"), "USD")},
        {"unit_price": Money(Decimal("2.30"), "EUR")},
        {
            "rounding": module.QuoteRoundingInput(
                unit_places=3, total_places=2, strategy="ROUND_HALF_UP"
            )
        },
        {"quote_fx_ref": "fx_new"},
        {"terms": ()},
        {"terms": original.terms + (term,)},
        {"terms": original.terms * 2},
        {"replaces_quote_id": "quote_old", "expected_quote_version": 1},
        {"scope_confirmation_id": "scope_other"},
        {"scope_confirmation_hash": "f" * 64},
    ]
    for change in changes:
        assert module.quote_creation_request_hash(intent(**change)) != digest, change
    assert module.quote_creation_request_hash(
        intent(terms=original.terms + (term,))
    ) != (module.quote_creation_request_hash(intent(terms=(term,) + original.terms)))
    assert (
        module.quote_creation_request_hash(
            intent(
                valid_until=original.valid_until.astimezone(
                    timezone(timedelta(hours=8))
                )
            )
        )
        == digest
    )


@pytest.mark.parametrize(
    "change",
    [
        {"valid_until": datetime(2026, 9, 1)},  # noqa: DTZ001 -- 验证拒绝无时区输入
        {"expected_context_hash": "A" * 64},
        {"prepared_by": " emp_bad"},
        {"scope_confirmation_id": "scope\x00bad"},
        {"replaces_quote_id": "quote_old"},
        {"expected_quote_version": 1},
        {"replaces_quote_id": "quote_old", "expected_quote_version": 0},
        {"unit_price": Money(Decimal(0), "USD")},
        {"terms": []},
    ],
)
def test_intent_rejects_malformed_or_partial_input(change: dict[str, object]) -> None:
    creation_module()
    with pytest.raises(ValidationError):
        intent(**change)


def test_rounding_and_terms_reject_unsupported_shape() -> None:
    module = creation_module()
    for values in [
        (13, 2, "ROUND_HALF_UP"),
        (2, -1, "ROUND_HALF_UP"),
        (2, 2, "unknown"),
    ]:
        with pytest.raises(ValidationError):
            module.QuoteRoundingInput(
                unit_places=values[0], total_places=values[1], strategy=values[2]
            )
    for kind, text in [
        ("contract_terms", "Anything"),
        ("payment_terms", ""),
        ("payment_terms", "bad\x00text"),
    ]:
        with pytest.raises(ValidationError):
            module.QuoteTerm(kind=kind, text=text)


def employee(role: str = "boss", **changes: object):
    """运行时受控员工，不作为真实身份来源。"""
    from shared.schemas.quote_facts import QuoteEmployeeFact
    return QuoteEmployeeFact(**({"tenant_id": TENANT, "employee_id": ACTOR,
        "role": role, "is_active": True, "manager_id": None, "team_id": None} | changes))


def issuer():
    """抬头是老板输入事实，未声称外部工商文件核验。"""
    from dataclasses import replace

    from domains.quotations import schemas
    from shared.schemas.provenance import SourceType
    assert hasattr(schemas, "QuoteIssuer"), "缺少抬头内部契约"
    p = replace(field("issuer").provenance, source_type=SourceType.EMPLOYEE_INPUT,
                source_id="issuer_test", source_quote=None)
    return schemas.QuoteIssuer(issuer_id="issuer_test", content_hash="f" * 64,
        name="Supplier Company", address="Test address", contact="hello@example.test",
        source_ref="issuer_test", confirmed_by=ACTOR, confirmed_at=p.confirmed_at,
        field_provenance={name: p for name in ("name", "address", "contact")})


def business_context(**changes: object):
    """所有标量从完整事实确定性投影，hash不由调用者临时填充。"""
    from domains.quotations import schemas, service
    from shared.schemas.quote_facts import QuoteRuntimeFacts
    assert hasattr(schemas, "QuoteBusinessContext"), "缺少完整业务上下文"
    facts = changes.pop("need_facts", bound_facts(material=field("steel"),
        size_spec=field("50 mm"), packaging=field("carton"), destination=field("US")))
    specification = service.quote_specification(facts)
    values = {"tenant_id": TENANT, "opportunity_id": "opp_test", "need_id": NEED,
        "account_id": "acct_test", "opportunity_state": "qualified", "owner_id": ACTOR,
        "prepared_by": ACTOR, "account_name": "Buyer", "country": "US",
        "category": "hinges", "specification": service.canonical_quote_specification(specification),
        "unit": "pieces", "destination": "US", "quantity": 500, "need_facts": facts,
        "need_facts_hash": demand_service.need_quote_facts_hash(facts),
        "specification_hash": service.quote_specification_hash(specification), "issuer": issuer(),
        "runtime": QuoteRuntimeFacts(current_actor=employee(), owner=employee(), preparer=employee())}
    return schemas.QuoteBusinessContext(**(values | changes))


@pytest.mark.parametrize("role", ["boss", "product", "sourcing", "finance", "sales", "manager", "viewer"])
@pytest.mark.parametrize("action", ["prepare", "read_internal"])
def test_quote_preparation_purpose_policy(role: str, action: str) -> None:
    from domains.quotations import service
    assert hasattr(service, "StrictQuotePreparationPolicy"), "缺少准备用途授权"
    from domains.quotations.errors import QuoteContextPermissionError
    policy = service.StrictQuotePreparationPolicy()
    if role in {"boss", "product", "sourcing", "finance"}:
        assert policy.require(TENANT, employee(role), action=action)
    else:
        with pytest.raises(QuoteContextPermissionError):
            policy.require(TENANT, employee(role), action=action)


def test_context_hash_binds_business_not_runtime_state() -> None:
    before = business_context()
    from shared.schemas.quote_facts import QuoteRuntimeFacts
    other = employee("finance", employee_id="emp_other")
    assert business_context(runtime=QuoteRuntimeFacts(current_actor=other,
        owner=employee(), preparer=employee()), opportunity_state="quoted").context_hash == before.context_hash
    for change in ({"account_name": "Buyer changed"}, {"country": "CA"},
                   {"prepared_by": "emp_other"}, {"issuer": issuer().model_copy(update={"content_hash": "e" * 64})}):
        if "prepared_by" in change:
            change["runtime"] = QuoteRuntimeFacts(current_actor=employee(), owner=employee(), preparer=other)
        assert business_context(**change).context_hash != before.context_hash
    for name in ("material", "packaging", "size_spec", "application"):
        updated = before.need_facts.model_copy(update={name: field("changed")})
        assert business_context(need_facts=updated).context_hash != before.context_hash
    with pytest.raises(ValidationError):
        business_context(context_hash="a" * 64)


@pytest.mark.parametrize("change", [{"quantity": 501}, {"unit": "boxes"},
    {"destination": "CA"}, {"category": "furniture"}, {"need_facts_hash": "0" * 64},
    {"specification": "50 mm"}, {"specification_hash": "0" * 64}, {"account_id": "acct_wrong"}])
def test_context_rejects_facts_scalar_mismatch(change: dict[str, object]) -> None:
    from domains.quotations import schemas
    assert hasattr(schemas, "QuoteBusinessContext"), "缺少完整业务上下文"
    from domains.quotations.errors import QuoteContextError
    with pytest.raises((ValidationError, QuoteContextError)):
        business_context(**change)
