"""目录簇评估与培养审批 workflow 的纯编排和恢复契约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from domains.approvals.catalog_contract import CatalogCultivationApprovalChange
from domains.approvals.service import (
    CATALOG_CULTIVATION_NAMESPACE,
    ApprovalState,
    CatalogApprovalContractError,
    CatalogApprovalFact,
    catalog_package_fields,
)
from domains.demand.service import CatalogEvidenceSummary, NeedClusterCatalogFacts
from domains.products.catalog_rules import (
    catalog_policy_content_hash,
    evaluate_catalog_facts,
)
from domains.products.permissions import ProductActor, ProductRole
from domains.products.schemas import (
    CatalogClusterFactsInput,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
)
from domains.products.service import CatalogProposalStateTransitionError
from shared.errors import TransientError, ValidationError
from shared.events.catalog import ApprovalDecided
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    ProspectAccountId,
    RunId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import SourceType
from workflows.engine.runner import StepStatus, WorkflowRun

try:
    from workflows.catalog_product_proposal import (
        CatalogCultivationApprovalDecidedHandler,
        build_catalog_evaluation_workflow_definition,
        build_catalog_evaluation_workflow_handlers,
        build_catalog_product_workflow_definition,
        build_catalog_product_workflow_handlers,
        catalog_cultivation_idempotency_key,
        catalog_evaluation_idempotency_key,
        cultivation_workflow_context,
        evaluation_workflow_context,
    )
    from workflows.catalog_product_proposal.mapping import (
        build_cultivation_approval_command,
        map_catalog_facts,
        require_exact_cultivation_approval,
    )
except (ImportError, ModuleNotFoundError):
    CatalogCultivationApprovalDecidedHandler = None  # type: ignore[assignment,misc]
    build_catalog_evaluation_workflow_definition = None  # type: ignore[assignment]
    build_catalog_evaluation_workflow_handlers = None  # type: ignore[assignment]
    build_catalog_product_workflow_definition = None  # type: ignore[assignment]
    build_catalog_product_workflow_handlers = None  # type: ignore[assignment]
    catalog_cultivation_idempotency_key = None  # type: ignore[assignment]
    catalog_evaluation_idempotency_key = None  # type: ignore[assignment]
    cultivation_workflow_context = None  # type: ignore[assignment]
    evaluation_workflow_context = None  # type: ignore[assignment]
    build_cultivation_approval_command = None  # type: ignore[assignment]
    map_catalog_facts = None  # type: ignore[assignment]
    require_exact_cultivation_approval = None  # type: ignore[assignment]


NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
TENANT = TenantId("tn_01K00000000000000000000000")
CLUSTER = NeedClusterId("ncl_01K0000000000000000000000")
POLICY_ID = CatalogProposalPolicyVersionId("cpv_01K00000000000000000000000")
EVALUATION_ID = CatalogProposalEvaluationId("cpe_01K00000000000000000000000")
PROPOSAL_ID = CatalogProductProposalId("cpr_01K00000000000000000000000")
APPROVAL_ID = ApprovalId("apr_01K00000000000000000000000")
OWNER = EmployeeId("emp_01K00000000000000000000000")
DECIDER = EmployeeId("emp_01K00000000000000000000001")
EVALUATION_RUN = RunId("run_01K00000000000000000000000")
WORKFLOW_RUN = RunId("run_01K00000000000000000000001")
FACTS_HASH = "a" * 64


def _content() -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


CONTENT_HASH = catalog_policy_content_hash(_content())


def _demand_facts(*, source_type: SourceType = SourceType.CONVERSATION) -> NeedClusterCatalogFacts:
    return NeedClusterCatalogFacts(
        tenant_id=TENANT,
        cluster_id=CLUSTER,
        cluster_category="three-wheelers",
        member_need_ids=(
            ValidatedNeedId("vnd_01K00000000000000000000000"),
            ValidatedNeedId("vnd_01K00000000000000000000001"),
            ValidatedNeedId("vnd_01K00000000000000000000002"),
        ),
        distinct_account_ids=(
            ProspectAccountId("acct_01K0000000000000000000000"),
            ProspectAccountId("acct_01K0000000000000000000001"),
            ProspectAccountId("acct_01K0000000000000000000002"),
        ),
        member_count=3,
        distinct_account_count=3,
        known_country_codes=(),
        unknown_country_account_count=3,
        recurring_true_account_count=0,
        recurring_false_account_count=0,
        recurring_unknown_account_count=3,
        quantity_unit_covered_account_count=0,
        unified_unit=None,
        safe_total_quantity=None,
        evidence_summaries=(
            CatalogEvidenceSummary(
                source_type=source_type,
                source_id=(
                    "msg_01K00000000000000000000000"
                    if source_type is SourceType.CONVERSATION
                    else "human-entered-country"
                ),
                extracted_by="human",
                confirmed_by=OWNER,
                confirmed_at=NOW,
                observed_at=NOW,
                content_hash="b" * 64,
            ),
        ),
        display_codes=(),
        facts_observed_at=NOW,
        facts_hash=FACTS_HASH,
    )


def _mapped() -> CatalogClusterFactsInput:
    assert map_catalog_facts is not None, "RED：Task 11 facts mapper 尚未实现"
    return map_catalog_facts(_demand_facts())


def _policy() -> CatalogProposalPolicyView:
    return CatalogProposalPolicyView(
        policy_version_id=POLICY_ID,
        content=_content(),
        content_hash=CONTENT_HASH,
        base_active_version_id=None,
        proposed_by=OWNER,
        approval_id=APPROVAL_ID,
        state="active",
        created_at=NOW,
        activated_at=NOW,
        terminal_at=None,
    )


def _evaluation() -> CatalogProposalEvaluationView:
    facts = _mapped()
    result = evaluate_catalog_facts(_content(), facts)
    return CatalogProposalEvaluationView(
        evaluation_id=EVALUATION_ID,
        cluster_id=CLUSTER,
        policy_version_id=POLICY_ID,
        facts_hash=FACTS_HASH,
        facts=facts,
        rule_results=result.rule_results,
        overall_passed=result.overall_passed,
        blocked_reason=result.blocked_reason,
        proposed_by_run=EVALUATION_RUN,
        created_at=NOW,
    )


def _proposal(state: str = "awaiting_approval_submission") -> CatalogProductProposalView:
    return CatalogProductProposalView(
        proposal_id=PROPOSAL_ID,
        evaluation_id=EVALUATION_ID,
        cluster_id=CLUSTER,
        policy_version_id=POLICY_ID,
        facts_hash=FACTS_HASH,
        owner_employee=OWNER,
        proposed_by_run=EVALUATION_RUN,
        approval_id=APPROVAL_ID if state != "awaiting_approval_submission" else None,
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


class _Demand:
    def __init__(self) -> None:
        self.facts = _demand_facts()

    async def get_cluster_catalog_facts(self, tenant_id, cluster_id):
        return self.facts


class _Products:
    def __init__(self) -> None:
        self.active: CatalogProposalPolicyView | None = _policy()
        self.evaluation = _evaluation()
        self.evaluation_result_override: object | None = None
        self.proposal = _proposal()
        self.evaluations: list[tuple[object, ...]] = []
        self.binds: list[tuple[object, ...]] = []
        self.decisions: list[Any] = []
        self.result_state = "cultivation_queued"
        self.result_override: object | None = None
        self.error: BaseException | None = None

    async def get_active_policy(self, tenant_id, *, actor):
        return self.active

    async def get_evaluation(self, tenant_id, evaluation_id, *, actor):
        return self.evaluation

    async def get_proposal(self, tenant_id, proposal_id, *, actor):
        return self.proposal

    async def evaluate_cluster(self, tenant_id, facts, *, proposed_by_run, actor):
        self.evaluations.append((tenant_id, facts, proposed_by_run, actor))
        if self.evaluation_result_override is not None:
            return self.evaluation_result_override
        return self.evaluation.model_copy(update={"proposed_by_run": proposed_by_run})

    async def bind_proposal_approval(
        self, tenant_id, proposal_id, approval_id, request_hash, *, actor
    ):
        self.binds.append((tenant_id, proposal_id, approval_id, request_hash, actor))
        self.proposal = self.proposal.model_copy(
            update={"approval_id": approval_id, "state": "pending_review"}
        )
        return self.proposal

    async def apply_cultivation_decision(
        self, tenant_id, proposal_id, decision, current_facts, *, actor
    ):
        if self.error is not None:
            raise self.error
        self.decisions.append((decision, current_facts))
        if self.result_override is not None:
            return self.result_override
        return self.proposal.model_copy(update={"state": self.result_state})


class _Approvals:
    def __init__(self) -> None:
        self.command: Any | None = None
        self.fact: CatalogApprovalFact | None = None
        self.read_error: CatalogApprovalContractError | None = None
        self.marked: list[tuple[object, ...]] = []
        self.failed: list[tuple[object, ...]] = []

    async def find_catalog_fact(self, tenant_id, change_set_ref):
        return self.fact

    async def submit_catalog(self, command):
        self.command = command
        self.fact = _approval_fact(command)
        return APPROVAL_ID

    async def read_catalog_fact(self, tenant_id, approval_id):
        if self.read_error is not None:
            raise self.read_error
        assert self.fact is not None
        return self.fact

    async def read_fact(self, tenant_id, approval_id):
        assert self.fact is not None
        return self.fact

    async def mark_applied(self, *args):
        self.marked.append(args)
        return True

    async def mark_apply_failed(self, *args):
        self.failed.append(args)

    async def expire_overdue(self, tenant_id):
        assert self.fact is not None
        return 1


def _approval_fact(command: Any, state: ApprovalState = ApprovalState.PENDING) -> CatalogApprovalFact:
    title, proposed, reason, blast, run, employee, evidence = catalog_package_fields(command)
    decided = state in {
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
        ApprovalState.APPLIED,
        ApprovalState.APPLY_FAILED,
    }
    return CatalogApprovalFact(
        tenant_id=TENANT,
        approval_id=APPROVAL_ID,
        approval_type="catalog_product_cultivation",
        contract_namespace=CATALOG_CULTIVATION_NAMESPACE,
        title=title,
        proposed_change=CatalogCultivationApprovalChange.model_validate(proposed),
        reason=reason,
        blast_radius=blast,
        proposed_by_run=run,
        proposed_by_employee=employee,
        owner_employee=OWNER,
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
        applied_at=NOW + timedelta(minutes=2) if state is ApprovalState.APPLIED else None,
        application_error_code=(
            "catalog_cultivation_apply_failed"
            if state is ApprovalState.APPLY_FAILED
            else None
        ),
    )


def _system() -> ProductActor:
    return ProductActor("system:catalog-products", ProductRole.SYSTEM, TENANT)


def _evaluation_run() -> WorkflowRun:
    assert evaluation_workflow_context is not None
    return WorkflowRun(
        run_id=WORKFLOW_RUN,
        tenant_id=TENANT,
        workflow_type="catalog_cluster_evaluation",
        workflow_version=1,
        subject_ref=str(CLUSTER),
        current_step="evaluate",
        status=StepStatus.RUNNING,
        created_at=NOW,
        context=evaluation_workflow_context(TENANT, _policy(), _demand_facts()),
    )


def _cultivation_run(step: str) -> WorkflowRun:
    assert cultivation_workflow_context is not None
    context = cultivation_workflow_context(
        TENANT, _proposal(), _evaluation(), _policy()
    )
    if step not in {"assemble_package", "submit_approval"}:
        context.update(
            approval_id=str(APPROVAL_ID), approval_timeout_seconds=3 * 24 * 60 * 60
        )
    return WorkflowRun(
        run_id=WORKFLOW_RUN,
        tenant_id=TENANT,
        workflow_type="catalog_product_cultivation",
        workflow_version=1,
        subject_ref=str(PROPOSAL_ID),
        current_step=step,
        status=StepStatus.RUNNING,
        created_at=NOW,
        context=context,
    )


def test_workflow_definitions_and_keys_are_exact() -> None:
    """若步骤图、workflow type 或确定性 key 漂移，本测试应失败。"""
    assert build_catalog_evaluation_workflow_definition is not None, "RED：评估 workflow 缺失"
    assert build_catalog_product_workflow_definition is not None, "RED：培养 workflow 缺失"
    evaluation = build_catalog_evaluation_workflow_definition()
    cultivation = build_catalog_product_workflow_definition()
    assert evaluation.workflow_type == "catalog_cluster_evaluation"
    assert tuple(step.step_name for step in evaluation.steps) == ("evaluate",)
    assert cultivation.workflow_type == "catalog_product_cultivation"
    assert tuple(step.step_name for step in cultivation.steps) == (
        "assemble_package",
        "submit_approval",
        "wait_decision",
        "apply_cultivation",
        "expire_proposal",
        "mark_applied",
    )
    assert cultivation.transitions == {
        "assemble_package": ("submit_approval",),
        "submit_approval": ("wait_decision",),
        "wait_decision": ("apply_cultivation", "expire_proposal"),
        "apply_cultivation": ("mark_applied",),
        "expire_proposal": (),
        "mark_applied": (),
    }
    assert catalog_evaluation_idempotency_key is not None
    assert catalog_cultivation_idempotency_key is not None
    assert catalog_evaluation_idempotency_key(TENANT, CLUSTER, POLICY_ID, FACTS_HASH) == (
        f"catalog-evaluation:{TENANT}:{CLUSTER}:{POLICY_ID}:{FACTS_HASH}"
    )
    assert catalog_cultivation_idempotency_key(TENANT, PROPOSAL_ID) == (
        f"catalog-cultivation:{TENANT}:{PROPOSAL_ID}"
    )


def test_mapping_is_explicit_strict_and_unsupported_evidence_fails_closed() -> None:
    """若 mapper 丢字段、放宽坏数据或支持无原件路由来源，本测试应失败。"""
    mapped = _mapped()
    assert mapped.tenant_id == TENANT
    assert mapped.member_need_ids == _demand_facts().member_need_ids
    assert mapped.evidence_summaries[0].source_type == "conversation"
    assert mapped.evidence_summaries[0].source_id.startswith("msg_")
    assert build_cultivation_approval_command is not None
    command = build_cultivation_approval_command(
        TENANT,
        _proposal(),
        _evaluation(),
        _policy(),
        NOW + timedelta(days=3),
    )
    _, proposed_change, *_ = catalog_package_fields(command)
    assert proposed_change["schema_version"] == "catalog-cultivation-v1"
    assert command.change_set_ref == f"catalog-cultivation:{PROPOSAL_ID}:{POLICY_ID}:{FACTS_HASH}"
    assert command.warning == "该提案不代表正式产品、已确认供应或客户可报价价格。"
    assert command.evidence_refs == (
        f"catalog-evidence-v1:conversation:msg_01K00000000000000000000000:{'b' * 64}",
    )
    with pytest.raises(ValidationError, match="Evidence"):
        bad_facts = map_catalog_facts(_demand_facts(source_type=SourceType.EMPLOYEE_INPUT))
        bad_result = evaluate_catalog_facts(_content(), bad_facts)
        bad_evaluation = _evaluation().model_copy(
            update={"facts": bad_facts, "rule_results": bad_result.rule_results}
        )
        build_cultivation_approval_command(
            TENANT,
            _proposal(),
            bad_evaluation,
            _policy(),
            NOW + timedelta(days=3),
        )


@pytest.mark.asyncio
async def test_evaluation_step_rereads_snapshot_and_uses_durable_run_id() -> None:
    """若评估信任 context body/Run ID 或不复核当前 hash，本测试应失败。"""
    assert build_catalog_evaluation_workflow_handlers is not None, "RED：评估 handlers 缺失"
    demand, products = _Demand(), _Products()
    handler = build_catalog_evaluation_workflow_handlers(
        demand, products, _system()
    )["catalog_product_evaluation.evaluate"]

    result = await handler.execute(_evaluation_run())

    assert result[0] == "complete"
    assert len(products.evaluations) == 1
    assert products.evaluations[0][2] == WORKFLOW_RUN
    assert set(result[2]) <= {"evaluation_id", "evaluation_state"}

    demand.facts = NeedClusterCatalogFacts(
        **{**_demand_facts().__dict__, "facts_hash": "f" * 64}
    )
    stale = await handler.execute(_evaluation_run())
    assert stale == ("complete", None, {"evaluation_state": "stale"})
    assert len(products.evaluations) == 1

    demand.facts = _demand_facts()
    products.evaluation_result_override = object()
    with pytest.raises(TransientError, match="目录簇评估结果暂不可用"):
        await handler.execute(_evaluation_run())


@pytest.mark.asyncio
async def test_submit_approval_ceil_timeout_exact_reuse_and_binding() -> None:
    """若 timeout floor、审批只按 ID 复用或遗漏 Products binding，本测试应失败。"""
    assert build_catalog_product_workflow_handlers is not None, "RED：培养 handlers 缺失"
    demand, products, approvals = _Demand(), _Products(), _Approvals()
    clock = lambda: NOW + timedelta(microseconds=1)
    handlers = build_catalog_product_workflow_handlers(
        demand, products, approvals, _system(), now=clock
    )
    result = await handlers["catalog_product_cultivation.submit"].execute(
        _cultivation_run("submit_approval")
    )

    assert result[0:2] == ("advance", "wait_decision")
    assert result[2]["approval_timeout_seconds"] == 3 * 24 * 60 * 60
    assert products.binds[0][1:3] == (PROPOSAL_ID, APPROVAL_ID)
    assert approvals.command is not None
    assert require_exact_cultivation_approval is not None
    require_exact_cultivation_approval(approvals.command, approvals.fact)


@pytest.mark.asyncio
async def test_products_commit_precedes_receipt_and_applied_replay_converges_products() -> None:
    """若 APPLIED receipt 跳过 Products 或先写 receipt，本测试应失败。"""
    assert build_catalog_product_workflow_handlers is not None
    demand, products, approvals = _Demand(), _Products(), _Approvals()
    command = build_cultivation_approval_command(
        TENANT, _proposal(), _evaluation(), _policy(), NOW + timedelta(days=3)
    )
    approvals.command = command
    approvals.fact = _approval_fact(command, ApprovalState.APPROVED)
    handlers = build_catalog_product_workflow_handlers(
        demand, products, approvals, _system(), now=lambda: NOW
    )

    applied = await handlers["catalog_product_cultivation.apply"].execute(
        _cultivation_run("apply_cultivation")
    )
    assert applied[0:2] == ("advance", "mark_applied")
    assert len(products.decisions) == 1
    assert approvals.marked == []
    receipt = await handlers["catalog_product_cultivation.mark_applied"].execute(
        _cultivation_run("mark_applied")
    )
    assert receipt[0] == "complete"
    assert len(products.decisions) == 2
    assert len(approvals.marked) == 1

    approvals.fact = _approval_fact(command, ApprovalState.APPLIED)
    replay = await handlers["catalog_product_cultivation.mark_applied"].execute(
        _cultivation_run("mark_applied")
    )
    assert replay[0] == "complete"
    assert len(products.decisions) == 3
    assert len(approvals.marked) == 1


@pytest.mark.asyncio
async def test_expiry_race_applies_products_then_repairs_receipt_inline() -> None:
    """若 expire 节点返回无效 successor，Products 提交后会遗失 applied 收据。"""
    demand, products, approvals = _Demand(), _Products(), _Approvals()
    products.proposal = _proposal("pending_review")
    command = build_cultivation_approval_command(
        TENANT, products.proposal, _evaluation(), _policy(), NOW + timedelta(days=3)
    )
    approvals.fact = _approval_fact(command, ApprovalState.APPROVED)
    handler = build_catalog_product_workflow_handlers(
        demand, products, approvals, _system(), now=lambda: NOW
    )["catalog_product_cultivation.expire"]

    result = await handler.execute(_cultivation_run("expire_proposal"))

    assert result == ("complete", None, {"application_state": "applied"})
    assert len(products.decisions) == 1
    assert len(approvals.marked) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("proposal_id", CatalogProductProposalId("cpr_01K00000000000000000000099")),
        ("approval_id", ApprovalId("apr_01K00000000000000000000099")),
        ("owner_employee", EmployeeId("emp_01K00000000000000000000099")),
        (
            "evaluation_id",
            CatalogProposalEvaluationId("cpe_01K00000000000000000000099"),
        ),
        ("cluster_id", NeedClusterId("ncl_01K0000000000000000000099")),
        (
            "policy_version_id",
            CatalogProposalPolicyVersionId("cpv_01K00000000000000000000099"),
        ),
        ("proposed_by_run", RunId("run_01K00000000000000000000099")),
        ("facts_hash", "f" * 64),
        ("created_at", NOW + timedelta(seconds=1)),
    ),
)
async def test_apply_result_must_match_every_immutable_proposal_binding(
    field: str, wrong_value: object
) -> None:
    """若只核对 proposal/facts，串错审批、owner 或来源评估仍会写 applied。"""
    demand, products, approvals = _Demand(), _Products(), _Approvals()
    products.proposal = _proposal("pending_review")
    command = build_cultivation_approval_command(
        TENANT, products.proposal, _evaluation(), _policy(), NOW + timedelta(days=3)
    )
    approvals.fact = _approval_fact(command, ApprovalState.APPROVED)
    products.result_override = products.proposal.model_copy(
        update={"state": "cultivation_queued", field: wrong_value}
    )
    handler = build_catalog_product_workflow_handlers(
        demand, products, approvals, _system(), now=lambda: NOW
    )["catalog_product_cultivation.apply"]

    with pytest.raises(TransientError, match="目录产品培养应用结果暂不可用"):
        await handler.execute(_cultivation_run("apply_cultivation"))

    assert approvals.marked == []
    assert approvals.failed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract_error", "expected_error", "message"),
    (
        (
            CatalogApprovalContractError("catalog_approval_not_found"),
            TransientError,
            "目录产品培养审批读取暂不可用",
        ),
        (
            CatalogApprovalContractError("catalog_contract_invalid"),
            ValidationError,
            "目录产品培养审批读取事实无效",
        ),
    ),
)
async def test_approval_not_found_retries_but_malformed_contract_is_permanent(
    contract_error: CatalogApprovalContractError,
    expected_error: type[Exception],
    message: str,
) -> None:
    """未读到事实可由跨事务可见性恢复，结构错误才是永久失败。"""
    demand, products, approvals = _Demand(), _Products(), _Approvals()
    products.proposal = _proposal("pending_review")
    approvals.read_error = contract_error
    handler = build_catalog_product_workflow_handlers(
        demand, products, approvals, _system(), now=lambda: NOW
    )["catalog_product_cultivation.wait"]

    with pytest.raises(expected_error, match=message):
        await handler.execute(_cultivation_run("wait_for_decision"))


@pytest.mark.asyncio
async def test_unknown_result_retries_and_exact_conflict_marks_only_approved_failed() -> None:
    """若未知提交结果误记失败，或 APPLIED 冲突被改写，本测试应失败。"""
    assert build_catalog_product_workflow_handlers is not None
    demand, products, approvals = _Demand(), _Products(), _Approvals()
    command = build_cultivation_approval_command(
        TENANT, _proposal(), _evaluation(), _policy(), NOW + timedelta(days=3)
    )
    approvals.fact = _approval_fact(command, ApprovalState.APPROVED)
    products.error = RuntimeError("private database context")
    handler = build_catalog_product_workflow_handlers(
        demand, products, approvals, _system(), now=lambda: NOW
    )["catalog_product_cultivation.apply"]
    with pytest.raises(TransientError) as failure:
        await handler.execute(_cultivation_run("apply_cultivation"))
    assert "private" not in str(failure.value)
    assert approvals.failed == []

    products.error = None
    products.result_override = object()
    with pytest.raises(TransientError):
        await handler.execute(_cultivation_run("apply_cultivation"))
    assert approvals.failed == []

    products.result_override = None
    products.error = CatalogProposalStateTransitionError("sensitive exact conflict")
    completed = await handler.execute(_cultivation_run("apply_cultivation"))
    assert completed[2]["application_error_code"] == "catalog_cultivation_apply_failed"
    assert len(approvals.failed) == 1

    approvals.fact = _approval_fact(command, ApprovalState.REJECTED)
    with pytest.raises(ValidationError, match="目录产品培养决定与 Products 状态冲突"):
        await handler.execute(_cultivation_run("apply_cultivation"))
    assert len(approvals.failed) == 1

    approvals.fact = _approval_fact(command, ApprovalState.APPLIED)
    with pytest.raises(TransientError):
        await handler.execute(_cultivation_run("apply_cultivation"))
    assert len(approvals.failed) == 1


class _HandlerEngine:
    def __init__(self) -> None:
        self.deliveries: list[tuple[object, ...]] = []

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        return WORKFLOW_RUN

    async def deliver_event(self, *args):
        self.deliveries.append(args)
        return True

    async def has_delivered_event(self, *args, **kwargs):
        return False


@pytest.mark.asyncio
async def test_approval_handler_filters_non_cultivation_before_strict_reader() -> None:
    """若共享 ApprovalDecided 被 Catalog strict reader 死信，本测试应失败。"""
    assert CatalogCultivationApprovalDecidedHandler is not None, "RED：培养审批事件 handler 缺失"
    engine, approvals = _HandlerEngine(), _Approvals()

    class _Legacy:
        approval_type = "playbook_change"

    approvals.fact = _Legacy()  # type: ignore[assignment]
    await CatalogCultivationApprovalDecidedHandler(engine, approvals).handle(
        ApprovalDecided(
            tenant_id=TENANT,
            occurred_at=NOW,
            approval_id=str(APPROVAL_ID),
            decision="approve",
            decided_by=DECIDER,
        )
    )
    assert engine.deliveries == []
