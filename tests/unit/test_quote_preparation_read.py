"""准备摘要必须区分正常缺项与损坏事实，不能改变原数量单位规则。"""

import importlib
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from domains.demand import service
from domains.demand.errors import NeedUnitError
from tests.unit.test_need_units import ACTOR, NEED, NOW, TENANT, bound_facts, field
from tests.unit.test_quote_context_contracts import business_context, employee


def assess(facts):
    """通过公开端口测试，不把尚未存在的模块导入错误当RED。"""
    assert hasattr(service, "assess_quote_preparation"), "缺少准备事实评估公共端口"
    return service.assess_quote_preparation(facts)


def unconfirmed(value):
    original = field(value)
    return replace(
        original,
        provenance=replace(original.provenance, confirmed_by=None, confirmed_at=None),
    )


@pytest.mark.parametrize(
    "changes,quantity_status,unit_status",
    [
        ({"quantity": None}, "missing", "blocked_by_quantity"),
        ({"quantity": field(0)}, "non_positive", "blocked_by_quantity"),
        ({"quantity": field(-1)}, "non_positive", "blocked_by_quantity"),
        ({"quantity": unconfirmed(500)}, "unconfirmed", "blocked_by_quantity"),
        ({"unit": None}, "current", "missing"),
        ({"unit_confirmation_id": None}, "current", "missing"),
        (
            {"unit": unconfirmed("pieces"), "unit_quantity_fact_hash": None},
            "current",
            "unconfirmed",
        ),
        ({"unit_quantity_fact_hash": None}, "current", "stale"),
        ({"quantity": field(600)}, "current", "stale"),
        ({}, "current", "current"),
    ],
)
def test_preparation_assesses_actual_quantity_and_unit(
    changes, quantity_status, unit_status
):
    facts = bound_facts().model_copy(update=changes)
    result = assess(facts)
    assert (result.quantity_status, result.unit_status) == (
        quantity_status,
        unit_status,
    )
    assert (result.tenant_id, result.need_id) == (TENANT, NEED)
    assert result.need_facts_hash == service.need_quote_facts_hash(facts)
    if facts.quantity is None:
        assert result.quantity_fact_hash is None
    else:
        assert result.quantity_fact_hash == service.quantity_fact_hash(
            TENANT, NEED, facts.quantity
        )


@pytest.mark.parametrize("value", [True, "500", 2.5])
def test_preparation_rejects_corrupt_quantity_instead_of_blocker(value):
    facts = bound_facts().model_copy(update={"quantity": field(value)})
    with pytest.raises(NeedUnitError) as error:
        assess(facts)
    assert error.value.code == "facts_corrupt"


def test_preparation_rejects_bypassed_provenance_validation():
    facts = bound_facts()
    corrupt = replace(facts.quantity.provenance)
    object.__setattr__(corrupt, "confirmed_at", None)
    with pytest.raises(NeedUnitError) as error:
        assess(
            facts.model_copy(
                update={"quantity": replace(facts.quantity, provenance=corrupt)}
            )
        )
    assert error.value.code == "facts_corrupt"


class PreparationLease:
    """仅持有fixture事实，无数据库或任何对象读取能力。"""

    def __init__(self, facts):
        self.facts, self.entries, self.active = facts, 0, False

    @asynccontextmanager
    async def open_preparation_facts(
        self, tenant_id, opportunity_id, actor_id, *, prepared_by
    ):
        assert (tenant_id, opportunity_id, actor_id, prepared_by) == (
            TENANT,
            "opp_test",
            ACTOR,
            ACTOR,
        )
        self.entries += 1
        self.active = True
        try:
            yield self.facts
        finally:
            self.active = False


def preparation_case(**changes):
    from domains.quotations import service as public

    assert hasattr(public, "QuotePreparationFacts"), "缺少可展示缺项的准备事实契约"
    c = business_context()
    fields = (
        "tenant_id",
        "opportunity_id",
        "account_id",
        "owner_id",
        "prepared_by",
        "account_name",
        "country",
        "opportunity_state",
        "need_facts",
        "runtime",
        "issuer",
    )
    facts = public.QuotePreparationFacts(
        **({name: getattr(c, name) for name in fields} | changes)
    )
    lease = PreparationLease(facts)
    impl = importlib.import_module("domains.quotations.preparation_read")
    adapter = importlib.import_module("workflows.quote_approval.preparation_facts")
    reader = impl.QuotePreparationReadServiceImpl(
        lease,
        public.StrictQuotePreparationPolicy(),
        adapter.DemandQuotePreparationProjector(),
        now=lambda: NOW,
    )
    return reader, lease, c


async def test_complete_preparation_uses_identical_business_hash_and_single_lease():
    reader, lease, original = preparation_case()
    value = await reader.get(TENANT, "opp_test", actor_id=ACTOR)
    assert value.context_hash == original.context_hash
    assert value.blockers == ()
    assert value.prepared_by == ACTOR
    assert lease.entries == 1 and not lease.active
    assert "source_quote" not in value.model_dump_json()
    assert "runtime" not in value.model_dump_json()


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"quantity": None}, [("quantity", "facts_missing")]),
        ({"quantity": field(0)}, [("quantity", "quantity_invalid")]),
        ({"quantity": unconfirmed(500)}, [("quantity", "fact_unconfirmed")]),
        ({"unit": None}, [("unit", "unit_missing")]),
        ({"unit": unconfirmed("pieces")}, [("unit", "fact_unconfirmed")]),
        ({"unit_quantity_fact_hash": None}, [("unit", "unit_stale")]),
        ({"destination": None}, [("destination", "facts_missing")]),
        (
            {"material": None, "size_spec": None, "application": None},
            [("specification", "facts_missing")],
        ),
    ],
)
async def test_missing_facts_keep_true_specification_but_no_full_context(
    changes, expected
):
    c = business_context()
    reader, _, _ = preparation_case(need_facts=c.need_facts.model_copy(update=changes))
    result = await reader.get(TENANT, "opp_test", actor_id=ACTOR)
    assert [(b.field, b.code) for b in result.blockers] == expected
    assert result.context_hash is None
    assert len(result.specification_hash) == 64


async def test_first_preparation_has_ordered_missing_unit_and_issuer():
    c = business_context()
    reader, _, _ = preparation_case(
        issuer=None, need_facts=c.need_facts.model_copy(update={"unit": None})
    )
    result = await reader.get(TENANT, "opp_test", actor_id=ACTOR)
    assert [(b.field, b.code) for b in result.blockers] == [
        ("unit", "unit_missing"),
        ("issuer", "issuer_missing"),
    ]
    assert result.issuer is None and result.context_hash is None


@pytest.mark.parametrize("role", ["sales", "manager", "viewer"])
async def test_preparation_does_not_grant_internal_facts_to_crm_roles(role):
    from domains.quotations.errors import QuoteContextPermissionError

    c = business_context()
    reader, lease, _ = preparation_case(
        runtime=c.runtime.model_copy(update={"current_actor": employee(role)})
    )
    with pytest.raises(QuoteContextPermissionError):
        await reader.get(TENANT, "opp_test", actor_id=ACTOR)
    assert not lease.active


@pytest.mark.parametrize(
    "changes",
    [
        {"product_category": None},
        {"material": field(" steel ")},
        {"quantity": field(True)},
    ],
)
async def test_corrupt_preparation_fails_instead_of_fabricating_summary(changes):
    from domains.quotations.errors import QuoteContextError

    c = business_context()
    reader, _, _ = preparation_case(need_facts=c.need_facts.model_copy(update=changes))
    with pytest.raises(QuoteContextError) as error:
        await reader.get(TENANT, "opp_test", actor_id=ACTOR)
    assert error.value.code == "facts_corrupt"


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tenant_other"},
        {"need_id": "need_other"},
        {"need_facts_hash": "b" * 64},
    ],
)
async def test_preparation_rejects_misbound_or_stale_assessment(change):
    from domains.quotations.errors import QuoteContextError

    reader, lease, c = preparation_case()

    class Projector:
        def project(self, facts):
            return service.assess_quote_preparation(c.need_facts).model_copy(
                update=change
            )

    reader._needs = Projector()
    with pytest.raises(QuoteContextError) as error:
        await reader.get(TENANT, "opp_test", actor_id=ACTOR)
    assert error.value.code == "facts_corrupt" and not lease.active


@pytest.mark.parametrize(
    "failure",
    [
        NeedUnitError("facts_corrupt"),
        NeedUnitError("quantity_invalid"),
        RuntimeError("private fixture detail"),
    ],
)
def test_preparation_adapter_only_maps_known_corruption(monkeypatch, failure):
    from domains.quotations.errors import (
        QuoteContextError,
        QuoteContextUnavailableError,
    )
    from workflows.quote_approval import preparation_facts

    def broken(_):
        raise failure

    monkeypatch.setattr(preparation_facts, "assess_quote_preparation", broken)
    expected = (
        QuoteContextError
        if getattr(failure, "code", None) == "facts_corrupt"
        else QuoteContextUnavailableError
    )
    with pytest.raises(expected) as error:
        preparation_facts.DemandQuotePreparationProjector().project(bound_facts())
    assert "private" not in str(error.value)


def test_preparation_adapter_preserves_original_cancellation(monkeypatch):
    import asyncio

    from workflows.quote_approval import preparation_facts

    cancel = asyncio.CancelledError()

    def broken(_):
        raise cancel

    monkeypatch.setattr(preparation_facts, "assess_quote_preparation", broken)
    with pytest.raises(asyncio.CancelledError) as error:
        preparation_facts.DemandQuotePreparationProjector().project(bound_facts())
    assert error.value is cancel
