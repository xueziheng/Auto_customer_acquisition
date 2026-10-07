"""Company Playbook 变更流程步骤；只编排公共域服务。"""

from __future__ import annotations

from typing import Any

from domains.approvals.schemas import ApprovalView
from domains.approvals.service import (
    ApprovalService,
    ApprovalState,
    ApprovalType,
    BlastRadius,
)
from domains.organization.errors import (
    PlaybookActivationConflictError,
    PlaybookApprovalFactInvalidError,
    PlaybookBaseVersionConflictError,
)
from domains.organization.permissions import OrganizationActor
from domains.organization.schemas import (
    PlaybookApprovalFact,
    PlaybookChangeSnapshot,
    PlaybookVersionView,
)
from domains.organization.service import OrganizationService
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import ApprovalId, PlaybookVersionId
from workflows.engine.runner import WorkflowRun

_APPROVAL_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
_STALE_WARNING = "候选基准已变化，批准后将进入 apply_failed，需基于最新版本重新提交"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"Playbook 工作流 {field} 无效")
    return value


def _identity(run: WorkflowRun) -> tuple[PlaybookVersionId, str, str]:
    version = _text(run.context.get("playbook_version_id"), "version_id")
    content_hash = _text(run.context.get("content_hash"), "content_hash")
    change_set_ref = _text(run.context.get("change_set_ref"), "change_set_ref")
    if version != run.subject_ref:
        raise ValidationError("Playbook 工作流主体与候选版本不匹配")
    if len(content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise ValidationError("Playbook 工作流 content_hash 无效")
    if change_set_ref != f"playbook:{version}:{content_hash}":
        raise ValidationError("Playbook 工作流变更集不匹配")
    return PlaybookVersionId(version), content_hash, change_set_ref


def _approval_id(run: WorkflowRun) -> ApprovalId:
    value = _text(run.context.get("approval_id"), "approval_id")
    if not value.startswith("apr_"):
        raise ValidationError("Playbook 工作流 approval_id 无效")
    return ApprovalId(value)


def _require_snapshot(
    run: WorkflowRun, snapshot: PlaybookChangeSnapshot
) -> PlaybookVersionView:
    version_id, content_hash, change_set_ref = _identity(run)
    candidate = snapshot.candidate
    if (
        candidate.playbook_version_id != version_id
        or candidate.content_hash != content_hash
        or candidate.change_set_ref != change_set_ref
    ):
        raise ValidationError("Playbook 候选快照与工作流上下文不匹配")
    return candidate


def _display(version: PlaybookVersionView) -> dict[str, object]:
    return {
        "version_id": str(version.playbook_version_id),
        "version_number": str(version.version_number),
        "company_type": version.company_type,
        "minimum_deal_amount": version.minimum_deal_amount,
        "minimum_deal_currency": version.minimum_deal_currency,
        "excluded_categories": list(version.excluded_categories),
        "sourcing_regions": list(version.sourcing_regions),
        "excluded_countries": list(version.excluded_countries),
        "monthly_budget_credits": (
            None
            if version.monthly_budget_credits is None
            else str(version.monthly_budget_credits)
        ),
        "approval_requirements": list(version.approval_requirements),
        "supply_capabilities_note": version.supply_capabilities_note,
    }


def _require_view(run: WorkflowRun, view: ApprovalView) -> None:
    approval_id = _approval_id(run)
    _, _, change_set_ref = _identity(run)
    if view.approval_id != str(approval_id) or view.change_set_ref != change_set_ref:
        raise ValidationError("Playbook 审批事实与工作流不匹配")


class AssemblePackageStep:
    def __init__(
        self, organization: OrganizationService, actor: OrganizationActor
    ) -> None:
        self._organization = organization
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        version_id, content_hash, change_set_ref = _identity(run)
        snapshot = await self._organization.get_change_snapshot(
            run.tenant_id, version_id, actor=self._actor
        )
        candidate = _require_snapshot(run, snapshot)
        return (
            "advance",
            "submit_approval",
            {
                "playbook_version_id": str(version_id),
                "content_hash": content_hash,
                "change_set_ref": change_set_ref,
                "proposed_by": str(candidate.proposed_by),
            },
        )


class SubmitApprovalStep:
    def __init__(
        self,
        organization: OrganizationService,
        approvals: ApprovalService,
        actor: OrganizationActor,
    ) -> None:
        self._organization = organization
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        version_id, _, change_set_ref = _identity(run)
        snapshot = await self._organization.get_change_snapshot(
            run.tenant_id, version_id, actor=self._actor
        )
        candidate = _require_snapshot(run, snapshot)
        reason = "老板提交了新的 Company Playbook 经营边界。"
        if not snapshot.base_is_current:
            reason = f"{reason}{_STALE_WARNING}"
        approval_id = await self._approvals.submit(
            run.tenant_id,
            ApprovalType.PLAYBOOK_CHANGE,
            f"激活 Company Playbook v{candidate.version_number}",
            {
                "before": "未配置"
                if snapshot.base is None
                else _display(snapshot.base),
                "after": _display(candidate),
            },
            reason,
            BlastRadius(
                affected_entities=[f"Company Playbook {version_id}"],
                if_approved="自动激活该精确候选版本；所有探索与门禁随后读取新版本。",
                if_rejected="当前生效版本保持不变；候选版本仅保留为审计历史。",
                reversible=True,
            ),
            proposed_by_employee=candidate.proposed_by,
            owner_employee=candidate.proposed_by,
            change_set_ref=change_set_ref,
        )
        return (
            "advance",
            "wait_decision",
            {
                "approval_id": str(approval_id),
                "approval_timeout_seconds": _APPROVAL_TIMEOUT_SECONDS,
                "change_set_ref": change_set_ref,
            },
        )


class WaitDecisionStep:
    def __init__(self, approvals: ApprovalService) -> None:
        self._approvals = approvals

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        approval_id = _approval_id(run)
        event = run.context.get("event")
        if event is not None:
            if (
                not isinstance(event, dict)
                or event.get("event_type") != "ApprovalDecided"
            ):
                raise ValidationError("Playbook 审批事件无效")
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("approval_id") != str(
                approval_id
            ):
                raise ValidationError("Playbook 审批事件关联失败")
        view = await self._approvals.get(run.tenant_id, approval_id)
        _require_view(run, view)
        if view.state == ApprovalState.APPROVED.value:
            return ("advance", "apply_playbook", {"approval_state": view.state})
        if view.state in {ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value}:
            return ("complete", None, {"approval_state": view.state})
        if view.state == ApprovalState.PENDING.value:
            return ("wait", None, {"approval_state": view.state})
        raise ValidationError("Playbook 审批状态不允许继续等待")


class ExpireApprovalStep:
    def __init__(self, approvals: ApprovalService) -> None:
        self._approvals = approvals

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        approval_id = _approval_id(run)
        await self._approvals.expire_overdue(run.tenant_id)
        view = await self._approvals.get(run.tenant_id, approval_id)
        _require_view(run, view)
        if view.state in {ApprovalState.EXPIRED.value, ApprovalState.REJECTED.value}:
            return ("complete", None, {"approval_state": view.state})
        if view.state == ApprovalState.APPROVED.value:
            return ("advance", "apply_playbook", {"approval_state": view.state})
        raise TransientError("Playbook 审批超时状态暂不可用")


class ApplyPlaybookStep:
    def __init__(
        self,
        organization: OrganizationService,
        approvals: ApprovalService,
        actor: OrganizationActor,
    ) -> None:
        self._organization = organization
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        version_id, _, change_set_ref = _identity(run)
        approval_id = _approval_id(run)
        view = await self._approvals.get(run.tenant_id, approval_id)
        _require_view(run, view)
        if (
            view.state != ApprovalState.APPROVED.value
            or view.decided_by_employee is None
            or view.decided_at is None
        ):
            raise ValidationError("Playbook 应用缺少已批准事实")
        approval = PlaybookApprovalFact(
            approval_id=approval_id,
            approval_type=view.approval_type,
            change_set_ref=change_set_ref,
            decided_by=view.decided_by_employee,
            decided_at=view.decided_at,
        )
        try:
            await self._organization.activate_playbook(
                run.tenant_id, version_id, approval, actor=self._actor
            )
        except PlaybookBaseVersionConflictError:
            return await self._failed(
                run, approval_id, "PLAYBOOK_BASE_VERSION_CONFLICT"
            )
        except (PlaybookApprovalFactInvalidError, PlaybookActivationConflictError):
            return await self._failed(
                run, approval_id, "PLAYBOOK_APPROVAL_FACT_INVALID"
            )
        return ("advance", "mark_applied", {"application_state": "activated"})

    async def _failed(
        self, run: WorkflowRun, approval_id: ApprovalId, code: str
    ) -> tuple[str, str | None, dict[str, Any]]:
        await self._approvals.mark_apply_failed(run.tenant_id, approval_id, code)
        return (
            "complete",
            None,
            {"application_state": "apply_failed", "application_error_code": code},
        )


class MarkAppliedStep:
    def __init__(self, approvals: ApprovalService) -> None:
        self._approvals = approvals

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _, content_hash, _ = _identity(run)
        await self._approvals.mark_applied(
            run.tenant_id,
            _approval_id(run),
            f"playbook-activate:{run.subject_ref}:{content_hash}",
        )
        return ("complete", None, {"application_state": "applied"})


__all__ = (
    "ApplyPlaybookStep",
    "AssemblePackageStep",
    "ExpireApprovalStep",
    "MarkAppliedStep",
    "SubmitApprovalStep",
    "WaitDecisionStep",
)
