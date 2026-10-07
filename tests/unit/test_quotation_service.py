"""服务门面当前身份与单一创建session规则；存储仅为受控端口。"""

import importlib
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from domains.quotations import schemas as q
from domains.quotations import service as public
from shared.schemas.identifiers import EmployeeId, OpportunityId, RunId, TenantId
from tests.unit.test_need_units import ACTOR, NOW, TENANT
from tests.unit.test_quote_context_contracts import employee


class UnusedApprovalContext:
    """T4非审批路径专用fail-on-use依赖，不能当审批真实provider。"""

    def open(self, tenant_id: TenantId, opportunity_id: OpportunityId, actor_id: EmployeeId,
             *, prepared_by: EmployeeId) -> AbstractAsyncContextManager[q.QuoteBusinessContext]:
        raise AssertionError("T4路径不得读取审批context")

    def open_approval_access(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, submitted_owner_id: EmployeeId
    ) -> AbstractAsyncContextManager[q.QuoteApprovalAccessContext]:
        raise AssertionError("T4路径不得读取审批access")

    def open_for_approval(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, decider_ids: tuple[EmployeeId, ...]
    ) -> AbstractAsyncContextManager[q.QuoteApprovalContext]:
        raise AssertionError("T4路径不得读取审批决策人")


class UnusedApprovalPolicy:
    """T4非审批路径不触发当前政策审批租约。"""

    def open(self, tenant_id: TenantId, category: str | None) -> AbstractAsyncContextManager[public.QuotePolicySelection]:
        raise AssertionError("T4路径不得读取审批政策")


class UnusedWorkflowRunReader:
    """T4非审批路径无技术executor，不给任何默认run。"""

    async def read(self, tenant_id: TenantId, run_id: RunId) -> q.QuoteWorkflowRunFact | None:
        raise AssertionError("T4路径不得读取审批run")


def service_case(*, fact=None):
    assert importlib.util.find_spec("domains.quotations.service_impl"), (
        "缺少报价服务实现"
    )
    actors = AsyncMock()
    actors.read_current.return_value = fact if fact is not None else employee()
    quotes = AsyncMock()
    quotes.get.return_value = None
    quotes.list_versions.return_value = ()
    quotes.get_by_operation.return_value = None

    @asynccontextmanager
    async def factory(tenant):
        assert tenant == TENANT
        uow = AsyncMock()
        uow.quotes = quotes
        yield uow

    impl = importlib.import_module(
        "domains.quotations.service_impl"
    ).QuotationServiceImpl
    svc = impl(
        factory,
        actors,
        public.StrictQuotePreparationPolicy(),
        AsyncMock(),
        context_provider=UnusedApprovalContext(),
        approval_policy_reader=UnusedApprovalPolicy(),
        workflow_run_reader=UnusedWorkflowRunReader(),
        now=lambda: NOW,
    )
    return svc, actors, quotes


@pytest.mark.parametrize("role", ["sales", "manager", "viewer"])
async def test_internal_read_does_not_expand_crm_roles(role):
    svc, _actors, quotes = service_case(fact=employee(role))
    from domains.quotations.errors import QuotationPermissionError

    with pytest.raises(QuotationPermissionError):
        await svc.get(
            TENANT, "absent", actor=q.QuotationActor(employee_id=ACTOR, role=role)
        )
    quotes.get.assert_not_awaited()


async def test_missing_actor_is_denied_before_existence_is_disclosed():
    svc, actors, quotes = service_case()
    actors.read_current.return_value = None
    from domains.quotations.errors import QuotationPermissionError

    with pytest.raises(QuotationPermissionError):
        await svc.get(
            TENANT, "absent", actor=q.QuotationActor(employee_id=ACTOR, role="boss")
        )
    quotes.get.assert_not_awaited()


async def test_authorized_missing_quote_is_fixed_error():
    svc, _, _ = service_case()
    from domains.quotations.errors import QuotationError

    with pytest.raises(QuotationError) as e:
        await svc.get(
            TENANT, "absent", actor=q.QuotationActor(employee_id=ACTOR, role="boss")
        )
    assert e.value.code == "quote_not_found"


async def test_session_requires_exact_preflight_and_convenience_reuses_gate():
    from domains.quotations.errors import QuotationError
    from tests.unit.test_quotation_contracts import basis_case

    svc, _, repo = service_case()
    assert hasattr(svc, "open_creation"), "缺少同事务创建session"
    i, b, c, _now = basis_case()
    repo.get_issuer.return_value = c.issuer
    actor = q.QuotationActor(employee_id=ACTOR, role="boss")
    async with svc.open_creation(TENANT, c.opportunity_id, c, actor=actor) as session:
        with pytest.raises(QuotationError) as e:
            await session.create_from_basis(i, b, operation_id=b.operation_id)
        assert e.value.code == "invalid_input"
        assert await session.preflight(i, operation_id=None) is None
        with pytest.raises(QuotationError):
            await session.create_from_basis(
                i.model_copy(update={"quote_fx_ref": "other"}),
                b,
                operation_id=b.operation_id,
            )
    repo.add.assert_not_awaited()
    created = await svc.create_from_basis(
        TENANT, i, b, c, operation_id=b.operation_id, actor=actor
    )
    assert created.state == q.QuoteState.DRAFT
    assert created.content.version == 1
    assert created.content.prepared_by == ACTOR


async def test_session_rechecks_actor_after_lock_wait():
    from domains.quotations.errors import QuotationPermissionError
    from tests.unit.test_quotation_contracts import basis_case

    svc, actors, repo = service_case()
    assert hasattr(svc, "open_creation"), "缺少同事务创建session"
    _i, _b, c, _now = basis_case()

    async def lock(*args):
        actors.read_current.return_value = employee(is_active=False)

    repo.lock_opportunity.side_effect = lock
    with pytest.raises(QuotationPermissionError):
        async with svc.open_creation(
            TENANT,
            c.opportunity_id,
            c,
            actor=q.QuotationActor(employee_id=ACTOR, role="boss"),
        ):
            pytest.fail("锁等待后失活必须拒绝")


@pytest.mark.parametrize(
    "state,replaces,error",
    [
        ("draft", False, "active_quote_exists"),
        ("expired", False, "revision_conflict"),
        ("accepted", True, "revision_conflict"),
        ("rejected", True, "revision_conflict"),
    ],
)
async def test_revision_preflight_rejects_illegal_replacement(state, replaces, error):
    from domains.quotations.errors import QuotationError
    from tests.unit.test_quotation_contracts import basis_case

    svc, _, repo = service_case()
    assert hasattr(svc, "open_creation"), "缺少同事务创建session"
    i, b, c, now = basis_case()
    old = public.build_quote_content(
        "old_quote", 1, i, b, c, created_at=now, replaced_quote_version=None
    )
    repo.list_versions.return_value = (
        q.QuoteDetailView(content=old, state=q.QuoteState(state)),
    )
    repo.get_issuer.return_value = c.issuer
    if replaces:
        i = i.model_copy(
            update={"replaces_quote_id": "old_quote", "expected_quote_version": 1}
        )
    with pytest.raises(QuotationError) as e:
        async with svc.open_creation(
            TENANT,
            c.opportunity_id,
            c,
            actor=q.QuotationActor(employee_id=ACTOR, role="boss"),
        ) as session:
            await session.preflight(i, operation_id=None)
    assert e.value.code == error


@pytest.mark.parametrize(
    "variant",
    [
        "missing",
        "mismatched_hash",
        "wrong_tenant",
        "future",
        "before_creation",
        "draft",
    ],
)
async def test_send_requires_exact_actual_reader_receipt_and_approved_state(variant):
    from datetime import timedelta

    from domains.quotations.errors import QuotationError
    from tests.unit.test_quotation_contracts import detail_fixture

    svc, _, repo = service_case()
    assert hasattr(svc, "record_verified_send"), "缺少真实回执发送门禁"
    quote = detail_fixture()
    receipt = q.QuoteSendReceipt(
        tenant_id=TENANT,
        attempt_id="attempt_test",
        quote_id=quote.content.quote_id,
        content_hash=quote.content.content_hash,
        sent_at=NOW,
    )
    repo.get.return_value = quote
    repo.send_receipt.return_value = None
    svc._send_reader.read.return_value = receipt
    if variant == "missing":
        svc._send_reader.read.return_value = None
    elif variant == "mismatched_hash":
        svc._send_reader.read.return_value = receipt.model_copy(
            update={"content_hash": "a" * 64}
        )
    elif variant == "wrong_tenant":
        receipt = receipt.model_copy(update={"tenant_id": "other"})
    elif variant == "future":
        receipt = receipt.model_copy(update={"sent_at": NOW + timedelta(seconds=1)})
        svc._send_reader.read.return_value = receipt
    elif variant == "before_creation":
        receipt = receipt.model_copy(update={"sent_at": NOW - timedelta(seconds=1)})
        svc._send_reader.read.return_value = receipt
    else:
        repo.get.return_value = quote.model_copy(update={"state": q.QuoteState.DRAFT})
    with pytest.raises(QuotationError):
        await svc.record_verified_send(
            TENANT,
            quote.content.quote_id,
            receipt,
            actor=q.QuotationActor(employee_id=ACTOR, role="boss"),
        )
    repo.add_send_receipt.assert_not_awaited()


async def test_valid_send_receipt_changes_state_once_and_replays_current_state():
    from tests.unit.test_quotation_contracts import detail_fixture

    svc, _, repo = service_case()
    assert hasattr(svc, "record_verified_send"), "缺少真实回执发送门禁"
    quote = detail_fixture()
    receipt = q.QuoteSendReceipt(
        tenant_id=TENANT,
        attempt_id="attempt_test",
        quote_id=quote.content.quote_id,
        content_hash=quote.content.content_hash,
        sent_at=NOW,
    )
    repo.get.return_value = quote
    repo.send_receipt.return_value = None
    svc._send_reader.read.return_value = receipt
    actor = q.QuotationActor(employee_id=ACTOR, role="boss")
    sent = await svc.record_verified_send(
        TENANT, quote.content.quote_id, receipt, actor=actor
    )
    assert sent.state == q.QuoteState.SENT
    repo.send_receipt.return_value = receipt
    repo.get.return_value = sent.model_copy(update={"state": q.QuoteState.EXPIRED})
    replay = await svc.record_verified_send(
        TENANT, quote.content.quote_id, receipt, actor=actor
    )
    assert replay.state == q.QuoteState.EXPIRED
    repo.add_send_receipt.assert_awaited_once()
    repo.transition.assert_awaited_once()


@pytest.mark.parametrize(
    "variant",
    [
        "inactive_after_lock",
        "expired_after_lock",
        "different_stored_receipt",
        "unavailable_reader",
    ],
)
async def test_send_lock_rechecks_and_dependency_fail_closed(variant):
    from domains.quotations.errors import (
        QuotationError,
        QuotationPermissionError,
        QuotationUnavailableError,
    )
    from tests.unit.test_quotation_contracts import detail_fixture

    svc, actors, repo = service_case()
    quote = detail_fixture()
    receipt = q.QuoteSendReceipt(
        tenant_id=TENANT,
        attempt_id="attempt_test",
        quote_id=quote.content.quote_id,
        content_hash=quote.content.content_hash,
        sent_at=NOW,
    )
    repo.get.return_value = quote
    repo.send_receipt.return_value = None
    svc._send_reader.read.return_value = receipt

    async def after_lock(*args):
        if variant == "inactive_after_lock":
            actors.read_current.return_value = employee(is_active=False)
        if variant == "expired_after_lock":
            svc._now = lambda: quote.content.valid_until

    repo.lock_opportunity.side_effect = after_lock
    if variant == "different_stored_receipt":
        repo.send_receipt.return_value = receipt.model_copy(
            update={"quote_id": "other"}
        )
    if variant == "unavailable_reader":
        svc._send_reader.read.side_effect = RuntimeError(
            "controlled dependency diagnostic"
        )
    with pytest.raises(
        (QuotationError, QuotationPermissionError, QuotationUnavailableError)
    ) as error:
        await svc.record_verified_send(
            TENANT,
            quote.content.quote_id,
            receipt,
            actor=q.QuotationActor(employee_id=ACTOR, role="boss"),
        )
    assert (
        error.value.code
        == {
            "inactive_after_lock": "permission_denied",
            "expired_after_lock": "quote_expired",
            "different_stored_receipt": "idempotency_conflict",
            "unavailable_reader": "dependency_unavailable",
        }[variant]
    )
    assert "diagnostic" not in str(error.value)
    repo.add_send_receipt.assert_not_awaited()


@pytest.mark.parametrize("limit", [True, 0, -1, 1001, "1"])
async def test_expiry_limit_is_strict_and_bounded(limit):
    from domains.quotations.errors import QuotationError

    svc, _, repo = service_case()
    with pytest.raises(QuotationError) as error:
        await svc.expire_overdue(TENANT, limit=limit)
    assert error.value.code == "invalid_input"
    repo.overdue_opportunities.assert_not_awaited()


async def test_creation_rechecks_clock_after_opportunity_lock():
    from domains.quotations.errors import QuotationError
    from tests.unit.test_quotation_contracts import basis_case

    svc, _, repo = service_case()
    i, b, c, _ = basis_case()
    repo.get_issuer.return_value = c.issuer

    async def after_lock(*args):
        svc._now = lambda: i.valid_until

    repo.lock_opportunity.side_effect = after_lock
    with pytest.raises(QuotationError) as error:
        await svc.create_from_basis(
            TENANT,
            i,
            b,
            c,
            operation_id=b.operation_id,
            actor=q.QuotationActor(employee_id=ACTOR, role="boss"),
        )
    assert error.value.code == "quote_expired"
    repo.add.assert_not_awaited()
