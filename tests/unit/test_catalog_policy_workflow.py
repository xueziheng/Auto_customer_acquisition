"""Catalog Policy 审批 workflow 的纯编排、严格映射与恢复契约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from domains.approvals.service import (
    CATALOG_POLICY_NAMESPACE,
    ApprovalState,
    CatalogApprovalFact,
    CatalogPolicyApprovalChange,
    catalog_package_fields,
)
from domains.products.catalog_rules import catalog_policy_content_hash
from domains.products.permissions import ProductActor, ProductRole
from domains.products.schemas import (
    CatalogPolicyChangeSnapshot,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
)
from domains.products.service import CatalogPolicyStateTransitionError
from shared.errors import TransientError, ValidationError
from shared.events.catalog import ApprovalDecided
from shared.schemas.identifiers import ApprovalId, EmployeeId, RunId, TenantId
from workflows.engine.runner import StepStatus, WorkflowRun

try:
    from workflows.catalog_product_proposal import (
        CatalogPolicyApprovalDecidedHandler,
        build_catalog_policy_workflow_definition,
        build_catalog_policy_workflow_handlers,
        catalog_policy_change_idempotency_key,
        policy_workflow_context,
    )
    from workflows.catalog_product_proposal.mapping import (
        build_policy_approval_command,
        require_exact_policy_approval,
    )
except (ImportError, ModuleNotFoundError):

    def _missing(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        pytest.fail("RED：Catalog Policy approval workflow 尚未实现")

    CatalogPolicyApprovalDecidedHandler = _missing
    build_catalog_policy_workflow_definition = _missing
    build_catalog_policy_workflow_handlers = _missing
    build_policy_approval_command = _missing
    catalog_policy_change_idempotency_key = _missing
    policy_workflow_context = _missing
    require_exact_policy_approval = _missing


NOW = datetime(2026, 9, 5, 8, tzinfo=UTC)
TENANT = TenantId("tn_01K00000000000000000000000")
POLICY_ID = "cpv_01K00000000000000000000000"
BASE_ID = "cpv_01K00000000000000000000001"
APPROVAL_ID = ApprovalId("apr_01K00000000000000000000000")
PROPOSER = EmployeeId("emp_01K00000000000000000000000")
DECIDER = EmployeeId("emp_01K00000000000000000000001")


def _content(accounts: int = 3) -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=accounts,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


HASH = catalog_policy_content_hash(_content())
BASE_HASH = catalog_policy_content_hash(_content(2))


def _policy(
    policy_id: str = POLICY_ID,
    *,
    content: CatalogProposalPolicyContent | None = None,
    content_hash: str = HASH,
    base_id: str | None = BASE_ID,
    state: str = "pending_approval",
) -> CatalogProposalPolicyView:
    return CatalogProposalPolicyView(
        policy_version_id=policy_id,
        content=content or _content(),
        content_hash=content_hash,
        base_active_version_id=base_id,
        proposed_by=PROPOSER,
        approval_id=None,
        state=state,
        created_at=NOW,
        activated_at=NOW if state == "active" else None,
        terminal_at=NOW if state in {"rejected", "expired", "stale"} else None,
    )


def _snapshot() -> CatalogPolicyChangeSnapshot:
    base = _policy(
        BASE_ID,
        content=_content(2),
        content_hash=BASE_HASH,
        base_id=None,
        state="active",
    )
    return CatalogPolicyChangeSnapshot(
        base=base,
        current=base,
        candidate=_policy(),
        base_is_current=True,
    )


def _system() -> ProductActor:
    return ProductActor("system:catalog-policy-workflow", ProductRole.SYSTEM, TENANT)


def _run(step: str, *, event: dict[str, object] | None = None) -> WorkflowRun:
    context = policy_workflow_context(TENANT, _snapshot())
    if step not in {"assemble_package", "submit_approval"}:
        context.update(
            approval_id=str(APPROVAL_ID),
            approval_timeout_seconds=7 * 24 * 60 * 60,
        )
    if event is not None:
        context["event"] = event
    return WorkflowRun(
        run_id=RunId("run_01K00000000000000000000000"),
        tenant_id=TENANT,
        workflow_type="catalog_proposal_policy_change",
        workflow_version=1,
        subject_ref=POLICY_ID,
        current_step=step,
        status=StepStatus.RUNNING,
        created_at=NOW,
        context=context,
    )


def _fact(
    command: Any, state: ApprovalState = ApprovalState.PENDING
) -> CatalogApprovalFact:
    title, proposed, reason, blast, run, employee, evidence = catalog_package_fields(
        command
    )
    decided = state in {
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
        ApprovalState.APPLIED,
        ApprovalState.APPLY_FAILED,
    }
    return CatalogApprovalFact(
        tenant_id=TENANT,
        approval_id=APPROVAL_ID,
        approval_type="catalog_proposal_policy_change",
        contract_namespace=CATALOG_POLICY_NAMESPACE,
        title=title,
        proposed_change=CatalogPolicyApprovalChange.model_validate(proposed),
        reason=reason,
        blast_radius=blast,
        proposed_by_run=run,
        proposed_by_employee=employee,
        owner_employee=PROPOSER,
        evidence_refs=tuple(evidence),
        change_set_ref=command.change_set_ref,
        created_at=NOW,
        expires_at=command.expires_at_limit,
        expires_at_limit=command.expires_at_limit,
        request_hash=command.request_hash,
        state=state,
        decided_by_employee=DECIDER if decided else None,
        decided_at=NOW + timedelta(minutes=1) if decided else None,
        decision_note=None,
        applied_at=NOW + timedelta(minutes=2)
        if state is ApprovalState.APPLIED
        else None,
        application_error_code=(
            "catalog_policy_apply_failed"
            if state is ApprovalState.APPLY_FAILED
            else None
        ),
    )


class _Products:
    def __init__(self) -> None:
        self.snapshot = _snapshot()
        self.binds: list[tuple[object, ...]] = []
        self.decisions: list[Any] = []
        self.result_state = "active"
        self.error: BaseException | None = None

    async def get_policy_change_snapshot(self, tenant_id, policy_version_id, *, actor):
        return self.snapshot

    async def bind_policy_approval(
        self, tenant_id, policy_version_id, approval_id, request_hash, *, actor
    ):
        self.binds.append(
            (tenant_id, policy_version_id, approval_id, request_hash, actor)
        )
        return self.snapshot.candidate.model_copy(update={"approval_id": approval_id})

    async def apply_policy_decision(
        self, tenant_id, policy_version_id, decision, *, actor
    ):
        if self.error is not None:
            raise self.error
        self.decisions.append(decision)
        return self.snapshot.candidate.model_copy(update={"state": self.result_state})


class _Approvals:
    def __init__(self) -> None:
        self.command: Any | None = None
        self.fact: CatalogApprovalFact | None = None
        self.submit_count = 0
        self.applied: list[tuple[ApprovalId, str]] = []
        self.failed: list[tuple[ApprovalId, str]] = []
        self.expire_count = 0

    async def find_catalog_fact(self, tenant_id, change_set_ref):
        return self.fact

    async def submit_catalog(self, command):
        self.submit_count += 1
        self.command = command
        self.fact = _fact(command)
        return APPROVAL_ID

    async def read_catalog_fact(self, tenant_id, approval_id):
        assert self.fact is not None
        return self.fact

    async def read_fact(self, tenant_id, approval_id):
        assert self.fact is not None
        return self.fact

    async def expire_overdue(self, tenant_id):
        self.expire_count += 1
        assert self.command is not None
        self.fact = _fact(self.command, ApprovalState.EXPIRED)
        return 1

    async def mark_applied(self, tenant_id, approval_id, idempotency_key):
        self.applied.append((approval_id, idempotency_key))
        return len(self.applied) == 1

    async def mark_apply_failed(self, tenant_id, approval_id, error):
        self.failed.append((approval_id, error))


class _Engine:
    def __init__(self) -> None:
        self.run_id = RunId("run_01K00000000000000000000000")
        self.active: RunId | None = self.run_id
        self.deliveries: list[tuple[object, ...]] = []
        self.already_delivered = False

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        return self.active

    async def deliver_event(self, tenant_id, run_id, event_type, payload):
        self.deliveries.append((tenant_id, run_id, event_type, payload))
        return True

    async def has_delivered_event(self, *args, **kwargs):
        return self.already_delivered


def test_public_approval_boundary_reexports_workflow_catalog_contracts() -> None:
    """移除 service 重导出会迫使 workflow 违规导入审批域内部。"""
    from domains.approvals import service

    assert service.CatalogPolicyApprovalCommand is not None
    assert service.CatalogPolicyContentFact is not None
    assert service.CatalogPolicyVersionFact is not None
    assert service.CatalogPolicyApprovalChange is not None
    assert service.catalog_package_fields is not None
    assert service.catalog_policy_request_hash is not None


def test_definition_has_exact_durable_policy_paths() -> None:
    """错步骤、默认批准超时或假轮询会破坏七天人工审批语义。"""
    definition = build_catalog_policy_workflow_definition()
    assert definition.workflow_type == "catalog_proposal_policy_change"
    assert definition.version == 1
    assert [item.step_name for item in definition.steps] == [
        "assemble_package",
        "submit_approval",
        "wait_decision",
        "apply_policy",
        "expire_policy",
        "mark_applied",
    ]
    wait = definition.steps[2]
    assert wait.wait_event_type == "ApprovalDecided"
    assert wait.timeout_context_key == "approval_timeout_seconds"
    assert wait.on_timeout == "expire_policy"
    assert wait.run_on_entry is True
    assert definition.transitions == {
        "assemble_package": ("submit_approval",),
        "submit_approval": ("wait_decision",),
        "wait_decision": ("apply_policy", "expire_policy"),
        "apply_policy": ("mark_applied",),
        "expire_policy": (),
        "mark_applied": (),
    }


def test_start_key_and_context_are_deterministic_and_metadata_only() -> None:
    """把策略正文或审批包塞入 Run 会越过 metadata-only 边界。"""
    context = policy_workflow_context(TENANT, _snapshot())
    assert catalog_policy_change_idempotency_key(TENANT, POLICY_ID) == (
        f"catalog-policy-change:{TENANT}:{POLICY_ID}"
    )
    assert context == {
        "policy_version_id": POLICY_ID,
        "content_hash": HASH,
        "change_set_ref": f"catalog-policy:{POLICY_ID}:{HASH}",
        "proposed_by": str(PROPOSER),
    }
    rendered = repr(context).casefold()
    assert all(
        marker not in rendered
        for marker in ("minimum_distinct", "before_policy", "provenance", "price")
    )


def test_mapping_explicitly_preserves_complete_policy_and_base() -> None:
    """漏映任一门槛或 base 会让审批承诺与 Products 创建承诺漂移。"""
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    assert command.content.minimum_distinct_accounts == 3
    assert command.content.minimum_recurring_accounts is None
    assert command.content.minimum_distinct_countries is None
    assert command.content.minimum_quantity_unit_accounts is None
    assert command.content.require_unified_unit is False
    assert command.base_active_version is not None
    assert command.base_active_version.policy_version_id == BASE_ID
    assert command.base_active_version.content.minimum_distinct_accounts == 2
    assert command.change_set_ref == f"catalog-policy:{POLICY_ID}:{HASH}"
    assert command.owner_employee == command.proposed_by_employee == PROPOSER


@pytest.mark.asyncio
async def test_submit_creates_or_recovers_exact_package_then_binds_products() -> None:
    """响应丢失后盲目新建或只比 approval_id 会错误复用审批。"""
    products = _Products()
    approvals = _Approvals()
    handlers = build_catalog_policy_workflow_handlers(
        products, approvals, _system(), now=lambda: NOW
    )

    first = await handlers["catalog_product_policy.submit"].execute(
        _run("submit_approval")
    )
    replay = await handlers["catalog_product_policy.submit"].execute(
        _run("submit_approval")
    )

    assert approvals.submit_count == 1
    assert first == replay
    assert first[0:2] == ("advance", "wait_decision")
    assert first[2]["approval_id"] == str(APPROVAL_ID)
    assert first[2]["approval_timeout_seconds"] == 604800
    assert len(products.binds) == 2
    assert products.binds[0][3] == approvals.command.request_hash
    assert set(first[2]) == {
        "approval_id",
        "approval_timeout_seconds",
        "change_set_ref",
    }


@pytest.mark.asyncio
async def test_submit_timeout_rounds_up_and_never_precedes_approval_expiry() -> None:
    """微秒级提交延迟不得把 durable wait deadline 向下截短。"""
    products = _Products()
    approvals = _Approvals()
    handlers = build_catalog_policy_workflow_handlers(
        products,
        approvals,
        _system(),
        now=lambda: NOW + timedelta(microseconds=1),
    )

    result = await handlers["catalog_product_policy.submit"].execute(
        _run("submit_approval")
    )

    assert result[2]["approval_timeout_seconds"] == 604800


def test_reusable_approval_validator_rejects_any_immutable_mismatch() -> None:
    """canonical 包任一事实改变都不能被同 ID 掩盖。"""
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    exact = _fact(command)
    assert require_exact_policy_approval(command, exact) == exact
    corruptions = (
        {"tenant_id": TenantId("tn_01K00000000000000000000009")},
        {"title": "被篡改"},
        {"owner_employee": DECIDER},
        {"request_hash": "f" * 64},
        {"evidence_refs": ("unexpected",)},
        {"expires_at_limit": NOW + timedelta(days=6)},
    )
    for mutation in corruptions:
        with pytest.raises(ValidationError):
            require_exact_policy_approval(command, exact.model_copy(update=mutation))


@pytest.mark.asyncio
async def test_wait_treats_event_as_wakeup_and_rechecks_canonical_decision() -> None:
    """伪造 event payload 不能代替中央持久审批事实。"""
    products = _Products()
    approvals = _Approvals()
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    approvals.command = command
    approvals.fact = _fact(command, ApprovalState.APPROVED)
    handlers = build_catalog_policy_workflow_handlers(
        products, approvals, _system(), now=lambda: NOW
    )
    run = _run(
        "wait_decision",
        event={
            "event_type": "ApprovalDecided",
            "payload": {
                "approval_id": str(APPROVAL_ID),
                "decision": "approve",
                "decided_by": str(DECIDER),
            },
        },
    )

    assert await handlers["catalog_product_policy.wait"].execute(run) == (
        "advance",
        "apply_policy",
        {"approval_state": "approved"},
    )
    approvals.fact = _fact(command, ApprovalState.PENDING)
    assert (await handlers["catalog_product_policy.wait"].execute(run))[0] == "wait"


@pytest.mark.asyncio
async def test_approved_applies_products_before_marking_approval() -> None:
    """先 mark applied 会在业务事务失败时伪造成功。"""
    products = _Products()
    approvals = _Approvals()
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    approvals.command = command
    approvals.fact = _fact(command, ApprovalState.APPROVED)
    handlers = build_catalog_policy_workflow_handlers(
        products, approvals, _system(), now=lambda: NOW
    )

    applied = await handlers["catalog_product_policy.apply"].execute(
        _run("apply_policy")
    )
    assert applied == (
        "advance",
        "mark_applied",
        {"application_state": "active"},
    )
    assert len(products.decisions) == 1
    assert approvals.applied == []
    decision = products.decisions[0]
    assert decision.state == "approved"
    assert decision.request_hash == command.request_hash
    assert decision.decided_by_employee == DECIDER

    marked = await handlers["catalog_product_policy.mark_applied"].execute(
        _run("mark_applied")
    )
    assert marked[0] == "complete"
    assert len(products.decisions) == 2
    assert approvals.applied == [
        (APPROVAL_ID, f"catalog-policy-apply:{POLICY_ID}:{HASH}")
    ]


@pytest.mark.asyncio
async def test_rejection_and_expiry_update_products_without_marking_applied() -> None:
    """拒绝/超时必须关闭候选，且绝不能伪装成 approval applied。"""
    products = _Products()
    approvals = _Approvals()
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    approvals.command = command
    approvals.fact = _fact(command, ApprovalState.REJECTED)
    products.result_state = "rejected"
    handlers = build_catalog_policy_workflow_handlers(
        products, approvals, _system(), now=lambda: NOW
    )
    rejected = await handlers["catalog_product_policy.apply"].execute(
        _run("apply_policy")
    )
    assert rejected == (
        "complete",
        None,
        {"application_state": "rejected"},
    )
    assert approvals.applied == []

    approvals.fact = _fact(command, ApprovalState.PENDING)
    products.result_state = "expired"
    expired = await handlers["catalog_product_policy.expire"].execute(
        _run("expire_policy")
    )
    assert expired == (
        "complete",
        None,
        {"application_state": "expired", "approval_state": "expired"},
    )
    assert approvals.expire_count == 1
    assert approvals.applied == []


@pytest.mark.asyncio
async def test_expire_step_retries_when_scheduler_arrives_before_canonical_expiry() -> (
    None
):
    """scheduler 提前触发且审批仍 pending 时不能把 workflow 永久失败。"""
    products = _Products()
    approvals = _Approvals()
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    approvals.command = command
    approvals.fact = _fact(command, ApprovalState.PENDING)

    async def keep_pending(tenant_id):
        del tenant_id
        approvals.expire_count += 1
        return 0

    approvals.expire_overdue = keep_pending  # type: ignore[method-assign]
    handlers = build_catalog_policy_workflow_handlers(
        products, approvals, _system(), now=lambda: NOW
    )

    with pytest.raises(TransientError) as caught:
        await handlers["catalog_product_policy.expire"].execute(
            _run("expire_policy")
        )

    assert str(caught.value) == "目录策略审批过期状态暂不可用"
    assert approvals.expire_count == 1
    assert products.decisions == []


@pytest.mark.asyncio
async def test_stale_or_known_business_conflict_uses_one_safe_failure_code() -> None:
    """确定性失败不得无限重试或把底层异常文本持久化。"""
    try:
        from domains.products.service import CatalogPolicyStateTransitionError
    except ImportError:
        CatalogPolicyStateTransitionError = RuntimeError  # type: ignore[misc,assignment]

    for result_state, error in (
        ("stale", None),
        (
            "active",
            CatalogPolicyStateTransitionError("database dsn and private detail"),
        ),
    ):
        products = _Products()
        approvals = _Approvals()
        command = build_policy_approval_command(
            TENANT, _snapshot(), NOW + timedelta(days=7)
        )
        approvals.command = command
        approvals.fact = _fact(command, ApprovalState.APPROVED)
        products.result_state = result_state
        products.error = error
        handlers = build_catalog_policy_workflow_handlers(
            products, approvals, _system(), now=lambda: NOW
        )
        result = await handlers["catalog_product_policy.apply"].execute(
            _run("apply_policy")
        )
        assert result == (
            "complete",
            None,
            {
                "application_state": "apply_failed",
                "application_error_code": "catalog_policy_apply_failed",
            },
        )
        assert approvals.failed == [(APPROVAL_ID, "catalog_policy_apply_failed")]
        assert "private detail" not in repr(result)


@pytest.mark.asyncio
async def test_transient_and_unknown_application_outcomes_remain_retryable() -> None:
    """未知提交结果不得错误落为永久 apply_failed。"""
    from shared.errors import InvalidStateTransition

    for error in (
        TransientError("provider timeout"),
        RuntimeError("private dsn"),
        InvalidStateTransition("generic deterministic error is not catalog-exact"),
    ):
        products = _Products()
        approvals = _Approvals()
        command = build_policy_approval_command(
            TENANT, _snapshot(), NOW + timedelta(days=7)
        )
        approvals.command = command
        approvals.fact = _fact(command, ApprovalState.APPROVED)
        products.error = error
        handlers = build_catalog_policy_workflow_handlers(
            products, approvals, _system(), now=lambda: NOW
        )
        with pytest.raises(TransientError) as caught:
            await handlers["catalog_product_policy.apply"].execute(_run("apply_policy"))
        assert approvals.failed == []
        assert "private dsn" not in str(caught.value)


@pytest.mark.asyncio
async def test_unknown_products_terminal_state_is_fixed_retryable_without_failure_receipt() -> (
    None
):
    """合法 Products view 的非预期状态仍是未知提交结果，不能永久失败。"""
    products = _Products()
    approvals = _Approvals()
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    approvals.command = command
    approvals.fact = _fact(command, ApprovalState.APPROVED)
    products.result_state = "pending_approval"
    handlers = build_catalog_policy_workflow_handlers(
        products, approvals, _system(), now=lambda: NOW
    )

    with pytest.raises(TransientError) as caught:
        await handlers["catalog_product_policy.apply"].execute(_run("apply_policy"))

    assert str(caught.value) == "目录策略应用结果暂不可用"
    assert approvals.failed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_state", "error"),
    [
        ("active", None),
        ("stale", None),
        pytest.param(
            "active",
            RuntimeError("private product detail"),
            id="unknown-product-error",
        ),
        pytest.param(
            "active",
            CatalogPolicyStateTransitionError("private exact conflict detail"),
            id="exact-product-conflict",
        ),
    ],
)
async def test_applied_receipt_still_converges_products_and_never_rewrites_failure(
    result_state: str,
    error: BaseException | None,
) -> None:
    """Approval receipt 不能反向证明 Products 已提交，也不能再改写为 apply_failed。"""
    products = _Products()
    approvals = _Approvals()
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    approvals.command = command
    approvals.fact = _fact(command, ApprovalState.APPLIED)
    products.result_state = result_state
    products.error = error
    handlers = build_catalog_policy_workflow_handlers(
        products, approvals, _system(), now=lambda: NOW
    )

    if result_state == "active" and error is None:
        assert await handlers["catalog_product_policy.mark_applied"].execute(
            _run("mark_applied")
        ) == ("complete", None, {"application_state": "applied"})
        assert len(products.decisions) == 1
    else:
        with pytest.raises(TransientError) as caught:
            await handlers["catalog_product_policy.mark_applied"].execute(
                _run("mark_applied")
            )
        assert str(caught.value) == "目录策略应用收据与 Products 状态暂不一致"
    assert approvals.applied == []
    assert approvals.failed == []


@pytest.mark.asyncio
async def test_decision_handler_ignores_unrelated_and_deduplicates_matching_event() -> (
    None
):
    """共享 ApprovalDecided 不能被投给错误 workflow，重复投递必须 no-op。"""
    command = build_policy_approval_command(
        TENANT, _snapshot(), NOW + timedelta(days=7)
    )
    approvals = _Approvals()
    approvals.command = command
    approvals.fact = _fact(command, ApprovalState.APPROVED)
    engine = _Engine()
    handler = CatalogPolicyApprovalDecidedHandler(engine, approvals)
    event = ApprovalDecided(TENANT, NOW, None, str(APPROVAL_ID), "approve", DECIDER)
    await handler.handle(event)
    assert engine.deliveries == [
        (
            TENANT,
            engine.run_id,
            "ApprovalDecided",
            {
                "approval_id": str(APPROVAL_ID),
                "decision": "approve",
                "decided_by": str(DECIDER),
            },
        )
    ]

    engine.active = None
    engine.already_delivered = True
    await handler.handle(event)
    approvals.fact = approvals.fact.model_copy(
        update={"approval_type": "catalog_product_cultivation"}
    )
    await handler.handle(event)
    assert len(engine.deliveries) == 1
