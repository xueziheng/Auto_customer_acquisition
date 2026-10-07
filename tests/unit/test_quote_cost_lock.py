"""完成回执绑定是纯守卫，不依赖实际quotation表或外部IO。"""

from decimal import Decimal

import pytest

from domains.costing import quote_lock
from domains.costing.errors import CostFreezeError
from shared.schemas.money import Money
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationOperationView,
    quote_creation_request_hash,
)
from tests.unit.test_need_units import NOW
from tests.unit.test_quote_context_contracts import intent


def operation():
    value = intent()
    return QuoteCreationOperationView(
        tenant_id=value.tenant_id,
        operation_id="op_test",
        idempotency_key="key",
        request_hash=quote_creation_request_hash(value),
        intent=value,
        basis_id="basis_test",
        state="frozen",
        created_at=NOW,
        completion=None,
        completed_at=None,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "other"},
        {"operation_id": "other"},
        {"request_hash": "f" * 64},
        {"basis_id": "other"},
        {"replaces_quote_id": "old", "replaced_quote_version": 1},
    ],
)
def test_receipt_rejects_every_wrong_binding(changes):
    assert hasattr(quote_lock, "require_completion"), "缺少完整回执绑定守卫"
    op = operation()
    receipt = QuoteCreationCompletion(
        **(
            {
                "tenant_id": op.tenant_id,
                "operation_id": op.operation_id,
                "request_hash": op.request_hash,
                "basis_id": op.basis_id,
                "quote_id": "quote_test",
                "quote_version": 1,
                "quote_content_hash": "a" * 64,
                "replaces_quote_id": None,
                "replaced_quote_version": None,
            }
            | changes
        )
    )
    with pytest.raises(CostFreezeError) as error:
        quote_lock.require_completion(op, receipt)
    assert error.value.code == "revision_conflict"


@pytest.mark.parametrize("version", [1, 2])
def test_receipt_accepts_exact_non_replacement_creation(version):
    assert hasattr(quote_lock, "require_completion"), "缺少完整回执绑定守卫"
    op = operation()
    receipt = QuoteCreationCompletion(
        tenant_id=op.tenant_id,
        operation_id=op.operation_id,
        request_hash=op.request_hash,
        basis_id=op.basis_id,
        quote_id="quote_test",
        quote_version=version,
        quote_content_hash="a" * 64,
        replaces_quote_id=None,
        replaced_quote_version=None,
    )
    assert quote_lock.require_completion(op, receipt) is None


@pytest.mark.parametrize("version", [1, 3])
def test_revision_receipt_still_rejects_wrong_successor_version(version):
    op = operation()
    revised = op.intent.model_copy(
        update={"replaces_quote_id": "old", "expected_quote_version": 1}
    )
    op = op.model_copy(
        update={"intent": revised, "request_hash": quote_creation_request_hash(revised)}
    )
    receipt = QuoteCreationCompletion(
        tenant_id=op.tenant_id,
        operation_id=op.operation_id,
        request_hash=op.request_hash,
        basis_id=op.basis_id,
        quote_id="new",
        quote_version=version,
        quote_content_hash="a" * 64,
        replaces_quote_id="old",
        replaced_quote_version=1,
    )
    with pytest.raises(CostFreezeError) as error:
        quote_lock.require_completion(op, receipt)
    assert error.value.code == "revision_conflict"


def test_completed_operation_requires_same_receipt_even_without_replacement():
    op = operation()
    receipt = QuoteCreationCompletion(
        tenant_id=op.tenant_id,
        operation_id=op.operation_id,
        request_hash=op.request_hash,
        basis_id=op.basis_id,
        quote_id="new",
        quote_version=2,
        quote_content_hash="a" * 64,
        replaces_quote_id=None,
        replaced_quote_version=None,
    )
    op = op.model_copy(
        update={"state": "completed", "completion": receipt, "completed_at": NOW}
    )
    with pytest.raises(CostFreezeError):
        quote_lock.require_completion(
            op, receipt.model_copy(update={"quote_version": 3})
        )


def test_frozen_basis_requires_distinct_cost_fx_snapshot():
    from domains.costing.schemas import FrozenCostBasis

    assert "cost_fx_rates" in FrozenCostBasis.model_fields, (
        "冻结依据缺少真实表内核算汇率"
    )
    assert FrozenCostBasis.model_fields["cost_fx_rates"].is_required()


def test_costing_unit_rejects_control_characters():
    from pydantic import ValidationError

    from domains.costing.schemas import CostingContext
    from tests.unit.test_quote_context_contracts import business_context
    from workflows.quote_approval.application import costing_context

    context = costing_context(business_context())
    values = {name: getattr(context, name) for name in CostingContext.model_fields}
    with pytest.raises(ValidationError):
        CostingContext(**(values | {"unit": "pcs\x00"}))


@pytest.mark.parametrize(
    "value",
    ["1E+100000", "1E-100000", "1E16", "1E-13", "0", "-1", "9" * 4097],
    ids=[
        "positive-exponent",
        "negative-exponent",
        "integer-overflow",
        "scale-overflow",
        "zero",
        "negative",
        "coefficient",
    ],
)
def test_manual_price_rejects_resource_and_numeric_input_limits(value):
    assert hasattr(quote_lock, "require_manual_price"), "人工单价缺少独立输入边界"
    with pytest.raises(CostFreezeError) as error:
        quote_lock.require_manual_price(Money(Decimal(value), "USD"))
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize(
    "value", ["9999999999999999.999999999999", "1E-12", "2.3000000000000000"]
)
def test_manual_price_accepts_numeric_boundaries_and_representation_zeros(value):
    assert hasattr(quote_lock, "require_manual_price"), "人工单价缺少独立输入边界"
    assert quote_lock.require_manual_price(Money(Decimal(value), "USD")) is None
