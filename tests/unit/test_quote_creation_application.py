"""创建应用必须先恢复真实报价，再考虑当前context；受控端口测试编排。"""

import importlib
from unittest.mock import AsyncMock, Mock

import pytest

from domains.quotations import schemas as q
from domains.quotations import service as public
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationOperationView,
)
from tests.unit.test_quotation_contracts import basis_case
from tests.unit.test_quote_context_contracts import employee


def test_cost_scope_public_export_for_typed_application_actor():
    from domains.costing import service

    assert hasattr(service, "CostingScope"), (
        "成本公开入口未导出scope，应用不能借内部导入旁路"
    )
    actor = service.CostingActor("emp_test", "boss", service.CostingScope.TENANT)
    assert actor.scope.value == "tenant"


def application_case():
    module = importlib.import_module("workflows.quote_approval.application")
    assert hasattr(module, "QuoteApplicationService"), "缺少真实报价创建/恢复应用"
    i, b, c, now = basis_case()
    detail = q.QuoteDetailView(
        content=public.build_quote_content(
            "quote_test", 1, i, b, c, created_at=now, replaced_quote_version=None
        ),
        state=q.QuoteState.DRAFT,
    )
    operation = QuoteCreationOperationView(
        tenant_id=i.tenant_id,
        operation_id=b.operation_id,
        idempotency_key="key",
        request_hash=b.request_hash,
        intent=i,
        basis_id=b.basis_id,
        state="frozen",
        created_at=now,
        completion=None,
        completed_at=None,
    )
    receipt = QuoteCreationCompletion(
        tenant_id=i.tenant_id,
        operation_id=b.operation_id,
        request_hash=b.request_hash,
        basis_id=b.basis_id,
        quote_id=detail.content.quote_id,
        quote_version=1,
        quote_content_hash=detail.content.content_hash,
        replaces_quote_id=None,
        replaced_quote_version=None,
    )
    costing = AsyncMock()
    costing.get_creation.return_value = operation
    costing.complete_creation.return_value = operation.model_copy(
        update={"state": "completed", "completion": receipt, "completed_at": now}
    )
    quotations = AsyncMock()
    quotations.get_by_operation.return_value = detail
    actors = AsyncMock()
    actors.read_current.return_value = employee()
    provider = Mock()
    provider.open.side_effect = AssertionError("已存报价恢复不能重开context")
    app = module.QuoteApplicationService(
        provider,
        costing,
        quotations,
        actors,
        public.StrictQuotePreparationPolicy(),
        now=lambda: now,
    )
    command = q.QuoteDraftCommand(
        **{n: getattr(i, n) for n in q.QuoteDraftCommand.model_fields}
    )
    return app, command, operation, detail, costing, quotations, actors


async def test_recovery_returns_persisted_content_before_any_context_or_freeze():
    app, command, operation, detail, costing, _quotations, _actors = application_case()
    value = await app.create(
        operation.tenant_id,
        command,
        actor_id=operation.intent.prepared_by,
        idempotency_key="key",
    )
    assert value == detail
    costing.freeze.assert_not_awaited()
    costing.complete_creation.assert_awaited_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"quote_fx_ref": "new"},
        {"terms": ()},
        {"expected_sheet_hash": "f" * 64},
        {"scope_confirmation_id": "other"},
        {"replaces_quote_id": "old", "expected_quote_version": 1},
    ],
)
async def test_same_key_cannot_change_any_original_intent(changes):
    from domains.quotations.errors import QuotationError

    app, command, operation, _detail, costing, _quotations, _actors = application_case()
    with pytest.raises(QuotationError) as e:
        await app.create(
            operation.tenant_id,
            command.model_copy(update=changes),
            actor_id=operation.intent.prepared_by,
            idempotency_key="key",
        )
    assert e.value.code == "idempotency_conflict"
    costing.complete_creation.assert_not_awaited()


async def test_completed_operation_without_real_quote_is_not_success():
    from domains.quotations.errors import QuotationUnavailableError

    app, command, operation, _detail, costing, quotations, _actors = application_case()
    costing.get_creation.return_value = costing.complete_creation.return_value
    quotations.get_by_operation.return_value = None
    with pytest.raises(QuotationUnavailableError) as e:
        await app.create(
            operation.tenant_id,
            command,
            actor_id=operation.intent.prepared_by,
            idempotency_key="key",
        )
    assert e.value.code == "storage_inconsistent"


async def test_other_current_cost_role_may_recover_but_not_finish_unwritten_quote():
    from domains.quotations.errors import QuotationPermissionError

    app, command, operation, _detail, _costing, quotations, actors = application_case()
    actors.read_current.return_value = employee("finance", employee_id="emp_finance")
    recovered = await app.create(
        operation.tenant_id, command, actor_id="emp_finance", idempotency_key="key"
    )
    assert recovered.content.prepared_by == operation.intent.prepared_by
    quotations.get_by_operation.return_value = None
    with pytest.raises(QuotationPermissionError):
        await app.create(
            operation.tenant_id, command, actor_id="emp_finance", idempotency_key="key"
        )
