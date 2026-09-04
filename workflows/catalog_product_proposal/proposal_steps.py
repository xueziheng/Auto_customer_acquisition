"""Catalog Product 培养审批步骤；业务效果提交后才写审批收据。"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from domains.approvals.service import (
    ApprovalService,
    ApprovalState,
    CatalogApprovalContractError,
    CatalogApprovalFact,
    CatalogCultivationApprovalCommand,
)
from domains.demand.service import DemandService
from domains.products.service import (
    CatalogApprovalDecisionInput,
    CatalogClusterFactsInput,
    CatalogCultivationConflictError,
    CatalogProductProposalView,
    CatalogProposalApprovalConflictError,
    CatalogProposalDecisionInvalidError,
    CatalogProposalEvaluationView,
    CatalogProposalNotFoundError,
    CatalogProposalPolicyView,
    CatalogProposalService,
    CatalogProposalStateTransitionError,
    ProductActor,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    RunId,
)
from workflows.catalog_product_proposal.mapping import (
    build_cultivation_approval_command,
    build_cultivation_approval_command_from_records,
    map_catalog_facts,
    require_exact_cultivation_approval,
)
from workflows.engine.runner import WorkflowRun

_APPROVAL_VALIDITY = timedelta(days=3)
_APPLY_FAILED_CODE = "catalog_cultivation_apply_failed"


def _text(value: object, field: str, *, prefix: str | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or (prefix is not None and not value.startswith(f"{prefix}_"))
    ):
        raise ValidationError(f"目录产品培养工作流 {field} 无效")
    return value


def _hash(value: object, field: str) -> str:
    checked = _text(value, field)
    if len(checked) != 64 or any(
        character not in "0123456789abcdef" for character in checked
    ):
        raise ValidationError(f"目录产品培养工作流 {field} 无效")
    return checked


def _identity(run: WorkflowRun) -> tuple[
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    NeedClusterId,
    CatalogProposalPolicyVersionId,
    str,
    str,
    EmployeeId,
    RunId,
]:
    proposal_id = _text(run.context.get("proposal_id"), "proposal_id", prefix="cpr")
    evaluation_id = _text(
        run.context.get("evaluation_id"), "evaluation_id", prefix="cpe"
    )
    cluster_id = _text(run.context.get("cluster_id"), "cluster_id", prefix="ncl")
    policy_id = _text(
        run.context.get("policy_version_id"), "policy_version_id", prefix="cpv"
    )
    policy_hash = _hash(run.context.get("policy_content_hash"), "policy_content_hash")
    facts_hash = _hash(run.context.get("facts_hash"), "facts_hash")
    owner = _text(run.context.get("owner_employee"), "owner_employee", prefix="emp")
    proposer = _text(run.context.get("proposed_by_run"), "proposed_by_run", prefix="run")
    expected_ref = f"catalog-cultivation:{proposal_id}:{policy_id}:{facts_hash}"
    if run.subject_ref != proposal_id or run.context.get("change_set_ref") != expected_ref:
        raise ValidationError("目录产品培养工作流主体无效")
    return (
        CatalogProductProposalId(proposal_id),
        CatalogProposalEvaluationId(evaluation_id),
        NeedClusterId(cluster_id),
        CatalogProposalPolicyVersionId(policy_id),
        policy_hash,
        facts_hash,
        EmployeeId(owner),
        RunId(proposer),
    )


def _approval_id(run: WorkflowRun) -> ApprovalId:
    return ApprovalId(_text(run.context.get("approval_id"), "approval_id", prefix="apr"))


def _expires_at_limit(run: WorkflowRun) -> datetime:
    if run.created_at.tzinfo is None or run.created_at.utcoffset() != timedelta(0):
        raise ValidationError("目录产品培养工作流创建时间无效")
    return run.created_at.astimezone(UTC) + _APPROVAL_VALIDITY


async def _dependency[T](operation: Callable[[], Awaitable[T]], subject: str) -> T:
    try:
        return await operation()
    except CatalogApprovalContractError as error:
        if error.code == "catalog_storage_unavailable":
            raise TransientError(f"{subject}暂不可用") from None
        raise ValidationError(f"{subject}事实无效") from None
    except TransientError:
        raise TransientError(f"{subject}暂不可用") from None
    except Exception:  # noqa: BLE001 -- 跨域异常固定脱敏
        raise TransientError(f"{subject}暂不可用") from None


async def _records(
    run: WorkflowRun,
    demand: DemandService,
    products: CatalogProposalService,
    actor: ProductActor,
) -> tuple[
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyView | None,
    CatalogClusterFactsInput,
]:
    (
        proposal_id,
        evaluation_id,
        cluster_id,
        policy_id,
        policy_hash,
        facts_hash,
        owner,
        proposer,
    ) = _identity(run)
    proposal = await _dependency(
        lambda: products.get_proposal(run.tenant_id, proposal_id, actor=actor),
        "目录产品提案读取",
    )
    evaluation = await _dependency(
        lambda: products.get_evaluation(run.tenant_id, evaluation_id, actor=actor),
        "目录产品评估读取",
    )
    policy = await _dependency(
        lambda: products.get_active_policy(run.tenant_id, actor=actor),
        "目录产品活动策略读取",
    )
    demand_facts = await _dependency(
        lambda: demand.get_cluster_catalog_facts(run.tenant_id, cluster_id),
        "目录产品 Demand 事实读取",
    )
    current_facts = map_catalog_facts(demand_facts)
    if (
        proposal.proposal_id != proposal_id
        or proposal.evaluation_id != evaluation_id
        or proposal.cluster_id != cluster_id
        or proposal.policy_version_id != policy_id
        or proposal.facts_hash != facts_hash
        or proposal.owner_employee != owner
        or proposal.proposed_by_run != proposer
        or evaluation.evaluation_id != evaluation_id
        or evaluation.cluster_id != cluster_id
        or evaluation.policy_version_id != policy_id
        or evaluation.facts_hash != facts_hash
        or evaluation.proposed_by_run != proposer
        or not evaluation.overall_passed
        or evaluation.blocked_reason is not None
        or current_facts.tenant_id != run.tenant_id
        or current_facts.cluster_id != cluster_id
    ):
        raise ValidationError("目录产品培养 canonical 事实无效")
    if policy is not None and policy.policy_version_id == policy_id and policy.content_hash != policy_hash:
        raise ValidationError("目录产品培养策略摘要无效")
    return proposal, evaluation, policy, current_facts


def _command(
    run: WorkflowRun,
    proposal: CatalogProductProposalView,
    evaluation: CatalogProposalEvaluationView,
) -> CatalogCultivationApprovalCommand:
    _, _, _, _, policy_hash, _, _, _ = _identity(run)
    return build_cultivation_approval_command_from_records(
        run.tenant_id,
        proposal,
        evaluation,
        policy_content_hash=policy_hash,
        expires_at_limit=_expires_at_limit(run),
    )


async def _approval(
    run: WorkflowRun,
    demand: DemandService,
    products: CatalogProposalService,
    approvals: ApprovalService,
    actor: ProductActor,
):
    proposal, evaluation, policy, current_facts = await _records(
        run, demand, products, actor
    )
    command = _command(run, proposal, evaluation)
    fact = await _dependency(
        lambda: approvals.read_catalog_fact(run.tenant_id, _approval_id(run)),
        "目录产品培养审批读取",
    )
    return (
        proposal,
        evaluation,
        policy,
        current_facts,
        require_exact_cultivation_approval(command, fact),
        command,
    )


def _decision(fact: CatalogApprovalFact) -> CatalogApprovalDecisionInput:
    state: Literal["approved", "rejected", "expired"]
    if fact.state in {ApprovalState.APPROVED, ApprovalState.APPLIED, ApprovalState.APPLY_FAILED}:
        state = "approved"
    elif fact.state is ApprovalState.REJECTED:
        state = "rejected"
    elif fact.state is ApprovalState.EXPIRED:
        state = "expired"
    else:
        raise ValidationError("目录产品培养审批尚未形成决定")
    try:
        return CatalogApprovalDecisionInput(
            approval_id=fact.approval_id,
            approval_type=fact.approval_type,
            contract_namespace=fact.contract_namespace,
            change_set_ref=fact.change_set_ref,
            request_hash=fact.request_hash,
            state=state,
            proposed_by_run=fact.proposed_by_run,
            proposed_by_employee=fact.proposed_by_employee,
            owner_employee=fact.owner_employee,
            decided_by_employee=fact.decided_by_employee,
            decided_at=fact.decided_at,
            expires_at=fact.expires_at,
        )
    except (TypeError, ValueError):
        raise ValidationError("目录产品培养审批决定事实无效") from None


async def _mark_failed(run: WorkflowRun, approvals: ApprovalService, approval_id: ApprovalId):
    await _dependency(
        lambda: approvals.mark_apply_failed(run.tenant_id, approval_id, _APPLY_FAILED_CODE),
        "目录产品培养审批失败记录",
    )
    return (
        "complete",
        None,
        {"application_state": "apply_failed", "application_error_code": _APPLY_FAILED_CODE},
    )


async def _apply(
    run: WorkflowRun,
    demand: DemandService,
    products: CatalogProposalService,
    approvals: ApprovalService,
    actor: ProductActor,
):
    proposal, _, _, current_facts, fact, _ = await _approval(
        run, demand, products, approvals, actor
    )
    if fact.state is ApprovalState.APPLY_FAILED:
        if fact.application_error_code != _APPLY_FAILED_CODE:
            raise ValidationError("目录产品培养审批应用失败码无效")
        return (
            "complete",
            None,
            {"application_state": "apply_failed", "application_error_code": _APPLY_FAILED_CODE},
        )
    decision = _decision(fact)
    try:
        result = await products.apply_cultivation_decision(
            run.tenant_id,
            proposal.proposal_id,
            decision,
            current_facts,
            actor=actor,
        )
    except (
        CatalogCultivationConflictError,
        CatalogProposalApprovalConflictError,
        CatalogProposalDecisionInvalidError,
        CatalogProposalStateTransitionError,
    ):
        if fact.state is ApprovalState.APPLIED:
            raise TransientError("目录产品培养收据与 Products 状态暂不一致") from None
        if fact.state is ApprovalState.APPROVED:
            return await _mark_failed(run, approvals, fact.approval_id)
        raise ValidationError("目录产品培养决定与 Products 状态冲突") from None
    except (CatalogProposalNotFoundError, TransientError):
        raise TransientError("目录产品培养应用暂不可用") from None
    except Exception:  # noqa: BLE001 -- 未知提交结果必须重放 Products
        raise TransientError("目录产品培养应用暂不可用") from None
    if (
        not isinstance(result, CatalogProductProposalView)
        or result.proposal_id != proposal.proposal_id
        or result.facts_hash != proposal.facts_hash
    ):
        raise TransientError("目录产品培养应用结果暂不可用")
    if decision.state == "approved":
        if result.state == "stale":
            if fact.state is ApprovalState.APPLIED:
                raise TransientError("目录产品培养收据与 Products 状态暂不一致")
            return ("complete", None, {"application_state": "stale"})
        if result.state == "cultivation_queued":
            if fact.state is ApprovalState.APPLIED:
                return ("complete", None, {"application_state": "applied"})
            return ("advance", "mark_applied", {"application_state": "cultivation_queued"})
    elif result.state == decision.state:
        return ("complete", None, {"application_state": result.state})
    if fact.state is ApprovalState.APPLIED:
        raise TransientError("目录产品培养收据与 Products 状态暂不一致")
    raise TransientError("目录产品培养应用结果暂不可用")


async def _mark_applied(run: WorkflowRun, approvals: ApprovalService, fact: CatalogApprovalFact):
    _, _, _, _, _, facts_hash, _, _ = _identity(run)
    await _dependency(
        lambda: approvals.mark_applied(
            run.tenant_id,
            fact.approval_id,
            f"catalog-cultivation-apply:{run.subject_ref}:{facts_hash}",
        ),
        "目录产品培养审批应用记录",
    )
    return ("complete", None, {"application_state": "applied"})


class AssemblePackageStep:
    def __init__(self, demand: DemandService, products: CatalogProposalService, actor: ProductActor) -> None:
        self._demand, self._products, self._actor = demand, products, actor

    async def execute(self, run: WorkflowRun):
        proposal, evaluation, policy, current = await _records(
            run, self._demand, self._products, self._actor
        )
        if (
            policy is None
            or policy.policy_version_id != proposal.policy_version_id
            or policy.proposed_by != proposal.owner_employee
            or current != evaluation.facts
        ):
            return ("complete", None, {"application_state": "stale"})
        build_cultivation_approval_command(
            run.tenant_id, proposal, evaluation, policy, _expires_at_limit(run)
        )
        return ("advance", "submit_approval", {})


class SubmitApprovalStep:
    def __init__(
        self,
        demand: DemandService,
        products: CatalogProposalService,
        approvals: ApprovalService,
        actor: ProductActor,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._demand, self._products, self._approvals = demand, products, approvals
        self._actor, self._now = actor, now

    async def execute(self, run: WorkflowRun):
        proposal, evaluation, policy, current = await _records(
            run, self._demand, self._products, self._actor
        )
        if (
            policy is None
            or policy.policy_version_id != proposal.policy_version_id
            or policy.proposed_by != proposal.owner_employee
            or current != evaluation.facts
        ):
            return ("complete", None, {"application_state": "stale"})
        command = build_cultivation_approval_command(
            run.tenant_id, proposal, evaluation, policy, _expires_at_limit(run)
        )
        existing = await _dependency(
            lambda: self._approvals.find_catalog_fact(run.tenant_id, command.change_set_ref),
            "目录产品培养审批恢复",
        )
        if existing is None:
            approval_id = await _dependency(
                lambda: self._approvals.submit_catalog(command),
                "目录产品培养审批提交",
            )
            fact = await _dependency(
                lambda: self._approvals.read_catalog_fact(run.tenant_id, approval_id),
                "目录产品培养审批读取",
            )
        else:
            fact, approval_id = existing, existing.approval_id
        require_exact_cultivation_approval(command, fact)
        bound = await _dependency(
            lambda: self._products.bind_proposal_approval(
                run.tenant_id,
                proposal.proposal_id,
                approval_id,
                command.request_hash,
                actor=self._actor,
            ),
            "目录产品培养审批绑定",
        )
        if bound.proposal_id != proposal.proposal_id or bound.approval_id != approval_id:
            raise ValidationError("目录产品培养审批绑定结果无效")
        remaining = math.ceil(
            (_expires_at_limit(run) - self._now().astimezone(UTC)).total_seconds()
        )
        if remaining <= 0:
            raise ValidationError("目录产品培养审批提交期限已过")
        return (
            "advance",
            "wait_decision",
            {"approval_id": str(approval_id), "approval_timeout_seconds": remaining},
        )


class WaitDecisionStep:
    def __init__(self, demand, products, approvals, actor) -> None:
        self._demand, self._products, self._approvals, self._actor = demand, products, approvals, actor

    async def execute(self, run: WorkflowRun):
        *_, fact, _ = await _approval(run, self._demand, self._products, self._approvals, self._actor)
        event = run.context.get("event")
        if event is not None:
            payload = event.get("payload") if isinstance(event, dict) else None
            if (
                not isinstance(event, dict)
                or event.get("event_type") != "ApprovalDecided"
                or not isinstance(payload, dict)
                or payload.get("approval_id") != str(fact.approval_id)
            ):
                raise ValidationError("目录产品培养审批事件无效")
        if fact.state is ApprovalState.PENDING:
            return ("wait", None, {"approval_state": "pending"})
        if fact.state is ApprovalState.EXPIRED:
            return ("advance", "expire_proposal", {"approval_state": "expired"})
        if fact.state in {ApprovalState.APPROVED, ApprovalState.REJECTED, ApprovalState.APPLIED, ApprovalState.APPLY_FAILED}:
            return ("advance", "apply_cultivation", {"approval_state": fact.state.value})
        raise ValidationError("目录产品培养审批状态无效")


class ApplyCultivationStep:
    def __init__(self, demand, products, approvals, actor) -> None:
        self._demand, self._products, self._approvals, self._actor = demand, products, approvals, actor

    async def execute(self, run: WorkflowRun):
        return await _apply(run, self._demand, self._products, self._approvals, self._actor)


class ExpireProposalStep(ApplyCultivationStep):
    async def execute(self, run: WorkflowRun):
        await _dependency(
            lambda: self._approvals.expire_overdue(run.tenant_id),
            "目录产品培养审批过期处理",
        )
        *_, fact, _ = await _approval(run, self._demand, self._products, self._approvals, self._actor)
        if fact.state is ApprovalState.PENDING:
            raise TransientError("目录产品培养审批过期状态暂不可用")
        return await _apply(run, self._demand, self._products, self._approvals, self._actor)


class MarkAppliedStep(ApplyCultivationStep):
    async def execute(self, run: WorkflowRun):
        result = await _apply(run, self._demand, self._products, self._approvals, self._actor)
        if result[0] != "advance" or result[1] != "mark_applied":
            return result
        *_, fact, _ = await _approval(run, self._demand, self._products, self._approvals, self._actor)
        return await _mark_applied(run, self._approvals, fact)


__all__ = (
    "ApplyCultivationStep",
    "AssemblePackageStep",
    "ExpireProposalStep",
    "MarkAppliedStep",
    "SubmitApprovalStep",
    "WaitDecisionStep",
)
