"""报价审批原子会话的规则测试；真实数据库事务与重启由integration覆盖。"""

import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from domains.quotations import schemas as q
from domains.quotations import service as public
from shared.schemas.quote_facts import QuoteEmployeeFact, QuoteRuntimeFacts
from tests.unit.test_quotation_contracts import basis_case
from tests.unit.test_quote_approval_contracts import DECIDER, QUOTE, RUN


def core_case():
    assert importlib.util.find_spec("domains.quotations.approval_service"), (
        "缺少单用途审批会话"
    )
    module = importlib.import_module("domains.quotations.approval_service")
    intent, basis, context, now = basis_case()
    quote = q.QuoteDetailView(
        content=public.build_quote_content(
            QUOTE,
            1,
            intent,
            basis,
            context,
            created_at=now,
            replaced_quote_version=None,
        ),
        state=q.QuoteState.DRAFT,
    )
    store = {"quote": quote, "bindings": (), "receipt": None}
    order = []
    repo = AsyncMock()
    repo.get.side_effect = lambda *args, **kwargs: store["quote"]
    repo.list_versions.return_value = (quote,)
    repo.approval_bindings.side_effect = lambda *args: store["bindings"]
    repo.approval_receipt.side_effect = lambda *args: store["receipt"]
    repo.add_approval_bindings.side_effect = lambda tenant, submission: store.update(
        bindings=submission.facts
    )
    repo.add_approval_receipt.side_effect = lambda tenant, receipt: store.update(
        receipt=receipt
    )

    async def transition(tenant, quote_id, expected, target, event):
        assert store["quote"].state == expected
        store["quote"] = store["quote"].model_copy(update={"state": target})
        return True

    repo.transition.side_effect = transition
    uow = AsyncMock()
    uow.quotes = repo
    uow.commit.side_effect = lambda: order.append("commit")

    @asynccontextmanager
    async def factory(tenant):
        assert tenant == context.tenant_id
        yield uow

    policies = AsyncMock()

    @asynccontextmanager
    async def policy_open(tenant, category):
        order.append("policy_open")
        selection = AsyncMock()
        selection.current.return_value = basis.policy
        try:
            yield selection
        finally:
            order.append("policy_close")

    policies.open = policy_open
    runs = AsyncMock()
    runs.read.return_value = q.QuoteWorkflowRunFact(
        tenant_id=context.tenant_id,
        run_id=RUN,
        workflow_type="quote_approval",
        workflow_version=1,
        subject_ref=QUOTE,
        quote_version=1,
        content_hash=quote.content.content_hash,
    )
    actor_check = AsyncMock(return_value=context.runtime.current_actor)
    core = module.QuoteApprovalServiceImpl(
        factory, AsyncMock(), policies, runs, actor_check, now=lambda: now
    )
    executor = q.QuoteWorkflowExecutor(
        workflow_type="quote_approval", run_id=RUN, quote_id=QUOTE
    )
    return core, executor, context, store, uow, runs, order


async def bind_case(core, executor, context):
    from shared.schemas.identifiers import ApprovalId

    actor = q.QuotationActor(
        employee_id=context.prepared_by, role=context.runtime.current_actor.role
    )
    async with core.open_approval(
        context.tenant_id, QUOTE, executor=executor
    ) as session:
        snapshot = await session.prepare_submission(context, actor=actor)
        facts = tuple(
            q.QuoteApprovalFact(
                tenant_id=context.tenant_id,
                approval_id=ApprovalId(f"apr_01K0000000000000000000000{i}"),
                approval_type=p.approval_type,
                change_set_ref=public.quote_change_set_ref(
                    QUOTE, p.content_hash, p.approval_type
                ),
                request_hash="a" * 64,
                payload=p,
                created_at=snapshot.internal_quote.content.created_at,
                expires_at=snapshot.expires_at_limit,
                expires_at_limit=snapshot.expires_at_limit,
                prepared_by=p.prepared_by,
                submitted_owner_id=p.submitted_owner_id,
                proposed_by_run=RUN,
                state="pending",
                decision=None,
                decided_by=None,
                decided_at=None,
                decision_note=None,
                applied_at=None,
                application_error_code=None,
            )
            for i, p in enumerate(snapshot.payloads)
        )
        submission = q.QuoteApprovalSubmission(
            tenant_id=context.tenant_id,
            quote_id=QUOTE,
            quote_version=1,
            content_hash=snapshot.internal_quote.content.content_hash,
            policy_id=snapshot.internal_quote.content.basis.policy_id,
            policy_hash=snapshot.internal_quote.content.basis.policy.content_hash,
            required_types=snapshot.required_types,
            facts=facts,
        )
        await session.bind(submission, context, actor=actor)
    return facts


async def test_session_binds_once_and_commits_before_policy_lease_release():
    core, executor, context, store, _, _, order = core_case()
    facts = await bind_case(core, executor, context)
    assert store["quote"].state == q.QuoteState.PENDING_APPROVAL
    assert len(store["bindings"]) == 3
    assert order[-2:] == ["commit", "policy_close"]
    async with core.open_approval(
        context.tenant_id, QUOTE, executor=executor
    ) as session:
        assert (await session.submission()).facts == facts


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow_type", "other"),
        ("workflow_version", 2),
        ("subject_ref", "quo_other"),
        ("quote_version", 2),
        ("content_hash", "f" * 64),
        ("tenant_id", "other"),
    ],
)
async def test_every_workflow_binding_field_is_checked(field, value):
    from domains.quotations.errors import QuoteApprovalError

    core, executor, context, _, _, runs, _ = core_case()
    runs.read.return_value = runs.read.return_value.model_copy(update={field: value})
    with pytest.raises(QuoteApprovalError) as error:
        await core.approval_target(context.tenant_id, QUOTE, executor=executor)
    assert error.value.code == "workflow_binding_invalid"


async def test_success_receipt_is_stable_across_application_state_progression():
    core, executor, context, store, uow, _, order = core_case()
    facts = await bind_case(core, executor, context)
    decider = QuoteEmployeeFact(
        tenant_id=context.tenant_id,
        employee_id=DECIDER,
        role="boss",
        is_active=True,
        manager_id=None,
        team_id=None,
    )
    business = context.model_copy(
        update={
            "runtime": QuoteRuntimeFacts(
                current_actor=decider,
                owner=context.runtime.owner,
                preparer=context.runtime.preparer,
            )
        }
    )
    full = q.QuoteApprovalContext(business=business, deciders=(decider,))
    approved = tuple(
        f.model_copy(
            update={
                "state": "approved",
                "decision": "approve",
                "decided_by": DECIDER,
                "decided_at": context.issuer.confirmed_at,
            }
        )
        for f in facts
    )
    async with core.open_approval(
        context.tenant_id, QUOTE, executor=executor
    ) as session:
        result = await session.apply(approved, full)
    assert result.outcome == "approved"
    assert result.receipt.approval_run_id == RUN
    assert store["quote"].state == q.QuoteState.APPROVED
    assert order[-2:] == ["commit", "policy_close"]
    assert uow.bus.publish.await_count == 1
    count = len(order)
    applied = tuple(
        f.model_copy(update={"state": "applied", "applied_at": f.decided_at})
        for f in approved
    )
    async with core.open_approval(
        context.tenant_id, QUOTE, executor=executor
    ) as session:
        second = await session.apply(applied, full)
    assert second.outcome == "already_applied"
    assert second.receipt == result.receipt
    assert "policy_open" not in order[count:]
    assert uow.bus.publish.await_count == 1


async def test_policy_selection_uses_fresh_clock_and_keeps_shared_lock():
    from datetime import timedelta

    from domains.costing import service as costing

    assert hasattr(costing, "CostingApprovalPolicyReader"), "缺少成本审批专用政策租约"
    module = importlib.import_module("domains.costing.approval_policy")
    now = [basis_case()[3]]
    order = []
    policies = AsyncMock()
    first = object()
    policies.get_effective.side_effect = lambda tenant, category, at: type(
        "Record", (), {"value": first}
    )()

    @asynccontextmanager
    async def factory(tenant):
        order.append("open")
        uow = AsyncMock()
        uow.policies = policies
        yield uow
        order.append("close")

    reader = module.CostingApprovalPolicyReaderImpl(factory, now=lambda: now[0])
    async with reader.open("tenant", "hinges") as lease:
        assert await lease.current() is first
        now[0] += timedelta(seconds=1)
        assert await lease.current() is first
        assert order == ["open"]
    assert order == ["open", "close"]
    assert policies.lock_selection.await_args.kwargs == {"exclusive": False}
    assert policies.get_effective.await_args.args[-1] == now[0]


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"quote_version": True, "content_hash": "a" * 64},
        {"quote_version": 0, "content_hash": "a" * 64},
        {"quote_version": 1, "content_hash": "bad"},
    ],
)
async def test_run_adapter_rejects_malformed_persistent_context(context):
    from domains.quotations.errors import QuoteApprovalError
    from workflows.engine.runner import StepStatus, WorkflowRun

    assert importlib.util.find_spec("workflows.quote_approval.run_reader")
    adapter = importlib.import_module("workflows.quote_approval.run_reader")
    engine = AsyncMock()
    engine.get_run.return_value = WorkflowRun(
        RUN,
        "tenant",
        "quote_approval",
        1,
        QUOTE,
        "complete",
        StepStatus.COMPLETED,
        basis_case()[3],
        context=context,
    )
    with pytest.raises(QuoteApprovalError) as error:
        await adapter.WorkflowQuoteRunReader(lambda: engine).read("tenant", RUN)
    assert error.value.code == "workflow_binding_invalid"


async def test_run_adapter_requires_published_engine_and_projects_only_metadata():
    from domains.quotations.errors import QuoteApprovalUnavailableError
    from workflows.engine.runner import StepStatus, WorkflowRun

    assert importlib.util.find_spec("workflows.quote_approval.run_reader")
    adapter = importlib.import_module("workflows.quote_approval.run_reader")
    with pytest.raises(QuoteApprovalUnavailableError) as error:
        await adapter.WorkflowQuoteRunReader(lambda: None).read("tenant", RUN)
    assert error.value.code == "dependency_unavailable"
    engine = AsyncMock()
    engine.get_run.return_value = WorkflowRun(
        RUN,
        "tenant",
        "quote_approval",
        1,
        QUOTE,
        "complete",
        StepStatus.COMPLETED,
        basis_case()[3],
        context={
            "quote_version": 1,
            "content_hash": "a" * 64,
            "secret": "must not pass",
        },
    )
    value = await adapter.WorkflowQuoteRunReader(lambda: engine).read("tenant", RUN)
    assert "secret" not in value.model_dump_json()
    assert value.quote_version == 1


async def test_policy_selection_uses_business_category_not_global_policy_category():
    from domains.quotations.errors import QuoteApprovalError

    core, executor, context, _, _, _, _ = core_case()
    context = context.model_copy(update={"category": "new-category"})
    calls = []
    original = core._policies.open

    def tracked(tenant, category):
        calls.append(category)
        return original(tenant, category)

    core._policies.open = tracked
    async with core.open_approval(
        context.tenant_id, QUOTE, executor=executor
    ) as session:
        try:
            await session.prepare_submission(
                context,
                actor=q.QuotationActor(
                    employee_id=context.prepared_by,
                    role=context.runtime.current_actor.role,
                ),
            )
        except QuoteApprovalError:
            pass
    assert calls == [context.category]


async def test_policy_adapter_preserves_explicit_policy_and_closes_lease():
    from domains.costing.schemas import PricingPolicyView
    from workflows.quote_approval.basis_adapter import _policy

    assert importlib.util.find_spec("workflows.quote_approval.policy_reader")
    adapter = importlib.import_module("workflows.quote_approval.policy_reader")
    value = PricingPolicyView.model_validate_json(
        basis_case()[1].policy.model_dump_json()
    )
    selection = AsyncMock()
    selection.current.return_value = value
    closed = []
    reader = AsyncMock()

    @asynccontextmanager
    async def opened(tenant, category):
        yield selection
        closed.append(True)

    reader.open = opened
    async with adapter.CostingQuoteApprovalPolicyReader(reader).open(
        "tenant", "hinges"
    ) as lease:
        assert await lease.current() == _policy(value)
        assert not closed
    assert closed


async def test_approval_adapter_reads_persistent_fact_and_maps_guard_role():
    from tests.unit.test_approval_service import quote_service_case, submit_quote

    assert importlib.util.find_spec("workflows.quote_approval.approvals")
    adapter = importlib.import_module("workflows.quote_approval.approvals")
    service, _, _, _ = quote_service_case()
    payload, approval_id = await submit_quote(service)
    facts = await adapter.read_quote_facts(service, payload.tenant_id, (approval_id,))
    assert facts[0].payload == payload
    assert facts[0].state == "pending"
    quotations = AsyncMock()

    @asynccontextmanager
    async def guard(tenant, subject, *, actor_id, action):
        yield q.QuoteApprovalAccessResult(can_decide=False, current_role="finance")

    quotations.open_approval_access = guard
    access = adapter.QuotationApprovalAccess(quotations)
    subject = access.subject(await service.read_fact(payload.tenant_id, approval_id))
    async with access.guard(
        subject, actor_id=payload.prepared_by, action="read"
    ) as result:
        assert result.current_role == "finance"


def test_quote_approved_outbox_has_only_safe_metadata_and_roundtrips():
    from infra.db.outbox import EVENT_REGISTRY, deserialize, serialize
    from shared.events.catalog import QuoteApproved

    event = QuoteApproved(
        tenant_id="tenant",
        occurred_at=basis_case()[3],
        quote_id=QUOTE,
        approved_by=DECIDER,
    )
    assert EVENT_REGISTRY.get("QuoteApproved") is QuoteApproved
    payload = serialize(event)
    assert set(payload) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "quote_id",
        "approved_by",
    }
    assert deserialize(QuoteApproved, payload) == event



