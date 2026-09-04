"""Catalog Policy 审批步骤；只编排 Products 与 Approvals 公共服务。"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from domains.approvals.service import (
    ApprovalService,
    ApprovalState,
    CatalogApprovalContractError,
    CatalogApprovalFact,
    CatalogPolicyApprovalCommand,
)
from domains.products.service import (
    CatalogApprovalDecisionInput,
    CatalogPolicyApprovalConflictError,
    CatalogPolicyChangeSnapshot,
    CatalogPolicyDecisionInvalidError,
    CatalogPolicyStateTransitionError,
    CatalogProposalService,
    ProductActor,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
)
from workflows.catalog_product_proposal.mapping import (
    build_policy_approval_command,
    require_exact_policy_approval,
)
from workflows.engine.runner import WorkflowRun

_APPROVAL_VALIDITY = timedelta(days=7)
_APPLY_FAILED_CODE = "catalog_policy_apply_failed"


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
    ):
        raise ValidationError(f"目录策略工作流 {field} 无效")
    return value


def _identity(
    run: WorkflowRun,
) -> tuple[CatalogProposalPolicyVersionId, str, str, EmployeeId]:
    policy_id = _text(run.context.get("policy_version_id"), "policy_version_id")
    content_hash = _text(run.context.get("content_hash"), "content_hash")
    change_set_ref = _text(run.context.get("change_set_ref"), "change_set_ref")
    proposed_by = _text(run.context.get("proposed_by"), "proposed_by")
    if policy_id != run.subject_ref or not policy_id.startswith("cpv_"):
        raise ValidationError("目录策略工作流主体与候选版本不匹配")
    if len(content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise ValidationError("目录策略工作流 content_hash 无效")
    if change_set_ref != f"catalog-policy:{policy_id}:{content_hash}":
        raise ValidationError("目录策略工作流变更集不匹配")
    if not proposed_by.startswith("emp_"):
        raise ValidationError("目录策略工作流提交人无效")
    return (
        CatalogProposalPolicyVersionId(policy_id),
        content_hash,
        change_set_ref,
        EmployeeId(proposed_by),
    )


def _approval_id(run: WorkflowRun) -> ApprovalId:
    value = _text(run.context.get("approval_id"), "approval_id")
    if not value.startswith("apr_"):
        raise ValidationError("目录策略工作流 approval_id 无效")
    return ApprovalId(value)


def _expires_at_limit(run: WorkflowRun) -> datetime:
    if run.created_at.tzinfo is None or run.created_at.utcoffset() != timedelta(0):
        raise ValidationError("目录策略工作流创建时间无效")
    return run.created_at.astimezone(UTC) + _APPROVAL_VALIDITY


async def _dependency[T](operation: Callable[[], Awaitable[T]], *, subject: str) -> T:
    """跨域未知失败统一为无上下文可重试错误。"""
    try:
        return await operation()
    except CatalogApprovalContractError as error:
        if error.code == "catalog_storage_unavailable":
            raise TransientError(f"{subject}暂不可用") from None
        raise ValidationError(f"{subject}事实无效") from None
    except TransientError:
        raise TransientError(f"{subject}暂不可用") from None
    except Exception:  # noqa: BLE001 -- 跨域异常文本不得进入 workflow 持久状态
        raise TransientError(f"{subject}暂不可用") from None


async def _snapshot(
    run: WorkflowRun, products: CatalogProposalService, actor: ProductActor
) -> CatalogPolicyChangeSnapshot:
    policy_id, content_hash, change_set_ref, proposed_by = _identity(run)
    snapshot = await _dependency(
        lambda: products.get_policy_change_snapshot(
            run.tenant_id, policy_id, actor=actor
        ),
        subject="目录策略候选读取",
    )
    candidate = snapshot.candidate
    if (
        candidate.policy_version_id != policy_id
        or candidate.content_hash != content_hash
        or candidate.proposed_by != proposed_by
        or f"catalog-policy:{candidate.policy_version_id}:{candidate.content_hash}"
        != change_set_ref
    ):
        raise ValidationError("目录策略候选快照与工作流上下文不匹配")
    return snapshot


async def _approval(
    run: WorkflowRun,
    products: CatalogProposalService,
    approvals: ApprovalService,
    actor: ProductActor,
) -> tuple[CatalogApprovalFact, CatalogPolicyApprovalCommand]:
    snapshot = await _snapshot(run, products, actor)
    command = build_policy_approval_command(
        run.tenant_id, snapshot, _expires_at_limit(run)
    )
    fact = await _dependency(
        lambda: approvals.read_catalog_fact(run.tenant_id, _approval_id(run)),
        subject="目录策略审批读取",
    )
    return require_exact_policy_approval(command, fact), command


def _decision(fact: CatalogApprovalFact) -> CatalogApprovalDecisionInput:
    state: Literal["approved", "rejected", "expired"]
    if fact.state in {
        ApprovalState.APPROVED,
        ApprovalState.APPLIED,
        ApprovalState.APPLY_FAILED,
    }:
        state = "approved"
    elif fact.state is ApprovalState.REJECTED:
        state = "rejected"
    elif fact.state is ApprovalState.EXPIRED:
        state = "expired"
    else:
        raise ValidationError("目录策略审批尚未形成可应用决定")
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
        raise ValidationError("目录策略审批决定事实无效") from None


async def _mark_failed(
    run: WorkflowRun, approvals: ApprovalService, approval_id: ApprovalId
) -> tuple[str, str | None, dict[str, Any]]:
    await _dependency(
        lambda: approvals.mark_apply_failed(
            run.tenant_id, approval_id, _APPLY_FAILED_CODE
        ),
        subject="目录策略审批失败记录",
    )
    return (
        "complete",
        None,
        {
            "application_state": "apply_failed",
            "application_error_code": _APPLY_FAILED_CODE,
        },
    )


async def _mark_applied_receipt(
    run: WorkflowRun,
    approvals: ApprovalService,
    fact: CatalogApprovalFact,
) -> tuple[str, str | None, dict[str, Any]]:
    _, content_hash, _, _ = _identity(run)
    await _dependency(
        lambda: approvals.mark_applied(
            run.tenant_id,
            fact.approval_id,
            f"catalog-policy-apply:{run.subject_ref}:{content_hash}",
        ),
        subject="目录策略审批应用记录",
    )
    return ("complete", None, {"application_state": "applied"})


async def _apply(
    run: WorkflowRun,
    products: CatalogProposalService,
    approvals: ApprovalService,
    actor: ProductActor,
    fact: CatalogApprovalFact,
) -> tuple[str, str | None, dict[str, Any]]:
    approval_id = _approval_id(run)
    if fact.state is ApprovalState.APPLY_FAILED:
        if fact.application_error_code != _APPLY_FAILED_CODE:
            raise ValidationError("目录策略审批应用失败码无效")
        return (
            "complete",
            None,
            {
                "application_state": "apply_failed",
                "application_error_code": _APPLY_FAILED_CODE,
            },
        )
    decision = _decision(fact)
    policy_id, _, _, _ = _identity(run)
    try:
        result = await products.apply_policy_decision(
            run.tenant_id, policy_id, decision, actor=actor
        )
    except (
        CatalogPolicyApprovalConflictError,
        CatalogPolicyDecisionInvalidError,
        CatalogPolicyStateTransitionError,
    ):
        if fact.state is ApprovalState.APPLIED:
            raise TransientError(
                "目录策略应用收据与 Products 状态暂不一致"
            ) from None
        return await _mark_failed(run, approvals, approval_id)
    except TransientError:
        if fact.state is ApprovalState.APPLIED:
            raise TransientError(
                "目录策略应用收据与 Products 状态暂不一致"
            ) from None
        raise TransientError("目录策略应用暂不可用") from None
    except Exception:  # noqa: BLE001 -- 未知提交结果必须重试且不得持久化原文
        if fact.state is ApprovalState.APPLIED:
            raise TransientError(
                "目录策略应用收据与 Products 状态暂不一致"
            ) from None
        raise TransientError("目录策略应用暂不可用") from None

    if decision.state == "approved":
        if result.state == "stale":
            if fact.state is ApprovalState.APPLIED:
                raise TransientError("目录策略应用收据与 Products 状态暂不一致")
            return await _mark_failed(run, approvals, approval_id)
        if result.state in {"active", "superseded"}:
            if fact.state is ApprovalState.APPLIED:
                return ("complete", None, {"application_state": "applied"})
            return (
                "advance",
                "mark_applied",
                {"application_state": result.state},
            )
    elif result.state == decision.state:
        return ("complete", None, {"application_state": result.state})
    if fact.state is ApprovalState.APPLIED:
        raise TransientError("目录策略应用收据与 Products 状态暂不一致")
    raise TransientError("目录策略应用结果暂不可用")


class AssemblePackageStep:
    def __init__(self, products: CatalogProposalService, actor: ProductActor) -> None:
        self._products = products
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        snapshot = await _snapshot(run, self._products, self._actor)
        return (
            "advance",
            "submit_approval",
            {
                "policy_version_id": str(snapshot.candidate.policy_version_id),
                "content_hash": snapshot.candidate.content_hash,
                "change_set_ref": run.context["change_set_ref"],
                "proposed_by": str(snapshot.candidate.proposed_by),
            },
        )


class SubmitApprovalStep:
    def __init__(
        self,
        products: CatalogProposalService,
        approvals: ApprovalService,
        actor: ProductActor,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._products = products
        self._approvals = approvals
        self._actor = actor
        self._now = now

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        policy_id, _, change_set_ref, _ = _identity(run)
        snapshot = await _snapshot(run, self._products, self._actor)
        expires_at_limit = _expires_at_limit(run)
        command = build_policy_approval_command(
            run.tenant_id, snapshot, expires_at_limit
        )
        existing = await _dependency(
            lambda: self._approvals.find_catalog_fact(run.tenant_id, change_set_ref),
            subject="目录策略审批恢复",
        )
        if existing is None:
            approval_id = await _dependency(
                lambda: self._approvals.submit_catalog(command),
                subject="目录策略审批提交",
            )
            fact = await _dependency(
                lambda: self._approvals.read_catalog_fact(run.tenant_id, approval_id),
                subject="目录策略审批读取",
            )
        else:
            fact = existing
            approval_id = fact.approval_id
        require_exact_policy_approval(command, fact)
        bound = await _dependency(
            lambda: self._products.bind_policy_approval(
                run.tenant_id,
                policy_id,
                approval_id,
                command.request_hash,
                actor=self._actor,
            ),
            subject="目录策略审批绑定",
        )
        if (
            bound.policy_version_id != policy_id
            or bound.content_hash != command.content_hash
            or bound.approval_id != approval_id
        ):
            raise ValidationError("目录策略审批绑定结果无效")
        remaining = math.ceil(
            (expires_at_limit - self._now().astimezone(UTC)).total_seconds()
        )
        if remaining <= 0:
            raise ValidationError("目录策略审批提交期限已过")
        return (
            "advance",
            "wait_decision",
            {
                "approval_id": str(approval_id),
                "approval_timeout_seconds": remaining,
                "change_set_ref": change_set_ref,
            },
        )


class WaitDecisionStep:
    def __init__(
        self,
        products: CatalogProposalService,
        approvals: ApprovalService,
        actor: ProductActor,
    ) -> None:
        self._products = products
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        fact, _ = await _approval(run, self._products, self._approvals, self._actor)
        event = run.context.get("event")
        if event is not None:
            if (
                not isinstance(event, dict)
                or event.get("event_type") != "ApprovalDecided"
            ):
                raise ValidationError("目录策略审批事件无效")
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("approval_id") != str(
                fact.approval_id
            ):
                raise ValidationError("目录策略审批事件关联失败")
        if fact.state is ApprovalState.PENDING:
            return ("wait", None, {"approval_state": "pending"})
        if fact.state is ApprovalState.EXPIRED:
            return ("advance", "expire_policy", {"approval_state": "expired"})
        if fact.state in {
            ApprovalState.APPROVED,
            ApprovalState.REJECTED,
            ApprovalState.APPLIED,
            ApprovalState.APPLY_FAILED,
        }:
            return (
                "advance",
                "apply_policy",
                {"approval_state": fact.state.value},
            )
        raise ValidationError("目录策略审批状态无效")


class ApplyPolicyStep:
    def __init__(
        self,
        products: CatalogProposalService,
        approvals: ApprovalService,
        actor: ProductActor,
    ) -> None:
        self._products = products
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        fact, _ = await _approval(run, self._products, self._approvals, self._actor)
        return await _apply(run, self._products, self._approvals, self._actor, fact)


class ExpirePolicyStep:
    def __init__(
        self,
        products: CatalogProposalService,
        approvals: ApprovalService,
        actor: ProductActor,
    ) -> None:
        self._products = products
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        await _dependency(
            lambda: self._approvals.expire_overdue(run.tenant_id),
            subject="目录策略审批过期处理",
        )
        fact, _ = await _approval(run, self._products, self._approvals, self._actor)
        if fact.state is ApprovalState.PENDING:
            raise TransientError("目录策略审批过期状态暂不可用")
        result = await _apply(run, self._products, self._approvals, self._actor, fact)
        if result[0] == "advance" and result[1] == "mark_applied":
            return await _mark_applied_receipt(run, self._approvals, fact)
        if fact.state is ApprovalState.EXPIRED and result[0] == "complete":
            return (
                "complete",
                None,
                {
                    "application_state": "expired",
                    "approval_state": "expired",
                },
            )
        return result


class MarkAppliedStep:
    def __init__(
        self,
        products: CatalogProposalService,
        approvals: ApprovalService,
        actor: ProductActor,
    ) -> None:
        self._products = products
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        fact, _ = await _approval(run, self._products, self._approvals, self._actor)
        result = await _apply(run, self._products, self._approvals, self._actor, fact)
        if result[0] != "advance" or result[1] != "mark_applied":
            return result
        return await _mark_applied_receipt(run, self._approvals, fact)


__all__ = (
    "ApplyPolicyStep",
    "AssemblePackageStep",
    "ExpirePolicyStep",
    "MarkAppliedStep",
    "SubmitApprovalStep",
    "WaitDecisionStep",
)
