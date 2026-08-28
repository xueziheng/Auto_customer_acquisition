"""完成回执绑定是纯守卫，不依赖实际quotation表或外部IO。"""

import pytest

from domains.costing import quote_lock
from domains.costing.errors import CostFreezeError
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
        {"quote_version": 2},
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


def test_receipt_accepts_exact_first_creation():
    assert hasattr(quote_lock, "require_completion"), "缺少完整回执绑定守卫"
    op = operation()
    receipt = QuoteCreationCompletion(
        tenant_id=op.tenant_id,
        operation_id=op.operation_id,
        request_hash=op.request_hash,
        basis_id=op.basis_id,
        quote_id="quote_test",
        quote_version=1,
        quote_content_hash="a" * 64,
        replaces_quote_id=None,
        replaced_quote_version=None,
    )
    assert quote_lock.require_completion(op, receipt) is None


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
