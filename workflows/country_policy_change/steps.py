"""国家政策包变更步骤；只编排合规域与审批域公共服务。"""

from __future__ import annotations

import json
from typing import Any

from domains.approvals.schemas import ApprovalView
from domains.approvals.service import (
    ApprovalService,
    ApprovalState,
    ApprovalType,
    BlastRadius,
)
from domains.compliance.errors import (
    CountryPolicyActivationConflictError,
    CountryPolicyApprovalFactInvalidError,
    CountryPolicyBaseVersionConflictError,
)
from domains.compliance.permissions import ComplianceActor
from domains.compliance.schemas import (
    CountryPolicyApprovalFact,
    CountryPolicyChangeSnapshot,
    CountryPolicyField,
    CountryPolicyVersionView,
)
from domains.compliance.service import ComplianceService
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import ApprovalId, CountryPolicyVersionId
from workflows.engine.runner import WorkflowRun

_APPROVAL_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
_STALE_WARNING = "候选基准已变化，批准后将进入 apply_failed，需基于最新版本重新提交"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"国家政策工作流 {field} 无效")
    return value


def _identity(run: WorkflowRun) -> tuple[CountryPolicyVersionId, str, str, str]:
    version = _text(run.context.get("country_policy_version_id"), "version_id")
    country_key = _text(run.context.get("country_key"), "country_key")
    content_hash = _text(run.context.get("content_hash"), "content_hash")
    change_set_ref = _text(run.context.get("change_set_ref"), "change_set_ref")
    if version != run.subject_ref or not version.startswith("cpp_"):
        raise ValidationError("国家政策工作流主体与候选版本不匹配")
    if len(content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise ValidationError("国家政策工作流 content_hash 无效")
    if change_set_ref != f"country_policy:{version}:{content_hash}":
        raise ValidationError("国家政策工作流变更集不匹配")
    return CountryPolicyVersionId(version), country_key, content_hash, change_set_ref


def _approval_id(run: WorkflowRun) -> ApprovalId:
    value = _text(run.context.get("approval_id"), "approval_id")
    if not value.startswith("apr_"):
        raise ValidationError("国家政策工作流 approval_id 无效")
    return ApprovalId(value)


def _require_snapshot(
    run: WorkflowRun, snapshot: CountryPolicyChangeSnapshot
) -> CountryPolicyVersionView:
    version_id, country_key, content_hash, change_set_ref = _identity(run)
    candidate = snapshot.candidate
    if (
        candidate.country_policy_version_id != version_id
        or candidate.country_key != country_key
        or candidate.content_hash != content_hash
        or candidate.change_set_ref != change_set_ref
    ):
        raise ValidationError("国家政策候选快照与工作流上下文不匹配")
    return candidate


def _display(version: CountryPolicyVersionView) -> dict[str, object]:
    return {
        "version_id": str(version.country_policy_version_id),
        "version_number": version.version_number,
        "country": version.country,
        "country_key": version.country_key,
        "public_research_allowed": version.public_research_allowed,
        "contact_enrichment_allowed": version.contact_enrichment_allowed,
        "cold_b2b_email_allowed": version.cold_b2b_email_allowed,
        "personal_data_basis_required": version.personal_data_basis_required,
        "subject_type_affects_judgment": version.subject_type_affects_judgment,
        "contact_type_affects_judgment": version.contact_type_affects_judgment,
        "opt_out_deadline_days": version.opt_out_deadline_days,
        "local_representative_required": version.local_representative_required,
        "requirements": list(version.requirements),
        "notes": version.notes,
    }


def _safe_sources(version: CountryPolicyVersionView) -> dict[str, dict[str, str]]:
    return {
        field.value: {
            "source_id": version.field_provenance[field].source_id,
            "source_type": version.field_provenance[field].source_type.value,
        }
        for field in sorted(CountryPolicyField, key=lambda item: item.value)
    }


def _require_view(run: WorkflowRun, view: ApprovalView) -> None:
    approval_id = _approval_id(run)
    _, _, _, change_set_ref = _identity(run)
    if view.approval_id != str(approval_id) or view.change_set_ref != change_set_ref:
        raise ValidationError("国家政策审批事实与工作流不匹配")


def _approval_display(proposed_change: dict[str, object]) -> dict[str, str]:
    return {
        key: (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
        for key, value in proposed_change.items()
    }


def _require_reusable_approval(
    view: ApprovalView,
    *,
    change_set_ref: str,
    proposed_by: str,
    title: str,
    proposed_change: dict[str, object],
    reason: str,
    blast_radius: BlastRadius,
    evidence_refs: list[str],
) -> ApprovalId:
    valid_states = {state.value for state in ApprovalState}
    if (
        not view.approval_id.startswith("apr_")
        or view.approval_type != ApprovalType.COUNTRY_POLICY_CHANGE.value
        or view.change_set_ref != change_set_ref
        or view.proposed_by != proposed_by
        or view.owner_name != proposed_by
        or view.state not in valid_states
        or view.title != title
        or view.proposed_change_display != _approval_display(proposed_change)
        or view.reason != reason
        or view.affected_entities != blast_radius.affected_entities
        or view.if_approved != blast_radius.if_approved
        or view.if_rejected != blast_radius.if_rejected
        or view.reversible is not blast_radius.reversible
        or view.evidence_links != evidence_refs
    ):
        raise ValidationError("国家政策既有审批事实与候选版本不匹配")
    return ApprovalId(view.approval_id)


class AssemblePackageStep:
    def __init__(self, compliance: ComplianceService, actor: ComplianceActor) -> None:
        self._compliance = compliance
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        version_id, country_key, content_hash, change_set_ref = _identity(run)
        snapshot = await self._compliance.get_change_snapshot(
            run.tenant_id, version_id, actor=self._actor
        )
        candidate = _require_snapshot(run, snapshot)
        return (
            "advance",
            "submit_approval",
            {
                "country_policy_version_id": str(version_id),
                "country_key": country_key,
                "content_hash": content_hash,
                "change_set_ref": change_set_ref,
                "proposed_by": str(candidate.proposed_by),
            },
        )


class SubmitApprovalStep:
    def __init__(
        self,
        compliance: ComplianceService,
        approvals: ApprovalService,
        actor: ComplianceActor,
    ) -> None:
        self._compliance = compliance
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        version_id, _, _, change_set_ref = _identity(run)
        snapshot = await self._compliance.get_change_snapshot(
            run.tenant_id, version_id, actor=self._actor
        )
        candidate = _require_snapshot(run, snapshot)
        proposed_by = _text(run.context.get("proposed_by"), "proposed_by")
        if proposed_by != str(candidate.proposed_by):
            raise ValidationError("国家政策提交人与候选版本不匹配")
        base_display: str | dict[str, object] = (
            "未配置" if snapshot.base is None else _display(snapshot.base)
        )
        current_display: str | dict[str, object] = (
            "未配置" if snapshot.current is None else _display(snapshot.current)
        )
        reason = "已提交逐字段人工核验来源的国家政策候选。"
        if not snapshot.base_is_current:
            reason = f"{reason}{_STALE_WARNING}"
        sources = _safe_sources(candidate)
        proposed_change: dict[str, object] = {
            "base": base_display,
            "current": current_display,
            "base_is_current": snapshot.base_is_current,
            "before": current_display,
            "after": _display(candidate),
            "field_sources": sources,
        }
        title = f"激活 {candidate.country} 国家政策包 v{candidate.version_number}"
        blast_radius = BlastRadius(
            affected_entities=[f"国家政策包 {candidate.country_key}"],
            if_approved="自动激活该精确候选版本；相关 Gateway 随后读取新政策。",
            if_rejected="当前生效政策保持不变；无生效政策时继续默认拒绝。",
            reversible=True,
        )
        evidence_refs = [source["source_id"] for source in sources.values()]
        existing = await self._approvals.get_by_change_set(
            run.tenant_id, change_set_ref
        )
        if existing is None:
            approval_id = await self._approvals.submit(
                run.tenant_id,
                ApprovalType.COUNTRY_POLICY_CHANGE,
                title,
                proposed_change,
                reason,
                blast_radius,
                proposed_by_employee=candidate.proposed_by,
                owner_employee=candidate.proposed_by,
                evidence_refs=evidence_refs,
                change_set_ref=change_set_ref,
            )
        else:
            approval_id = _require_reusable_approval(
                existing,
                change_set_ref=change_set_ref,
                proposed_by=proposed_by,
                title=title,
                proposed_change=proposed_change,
                reason=reason,
                blast_radius=blast_radius,
                evidence_refs=evidence_refs,
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
                raise ValidationError("国家政策审批事件无效")
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("approval_id") != str(
                approval_id
            ):
                raise ValidationError("国家政策审批事件关联失败")
        view = await self._approvals.get(run.tenant_id, approval_id)
        _require_view(run, view)
        if view.state == ApprovalState.APPROVED.value:
            return ("advance", "apply_policy", {"approval_state": view.state})
        if view.state in {ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value}:
            return ("complete", None, {"approval_state": view.state})
        if view.state == ApprovalState.PENDING.value:
            return ("wait", None, {"approval_state": view.state})
        raise ValidationError("国家政策审批状态不允许继续等待")


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
            return ("advance", "apply_policy", {"approval_state": view.state})
        raise TransientError("国家政策审批超时状态暂不可用")


class ApplyPolicyStep:
    def __init__(
        self,
        compliance: ComplianceService,
        approvals: ApprovalService,
        actor: ComplianceActor,
    ) -> None:
        self._compliance = compliance
        self._approvals = approvals
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        version_id, _, _, change_set_ref = _identity(run)
        approval_id = _approval_id(run)
        view = await self._approvals.get(run.tenant_id, approval_id)
        _require_view(run, view)
        if (
            view.state != ApprovalState.APPROVED.value
            or view.decided_by_employee is None
            or view.decided_at is None
        ):
            raise ValidationError("国家政策应用缺少已批准事实")
        approval = CountryPolicyApprovalFact(
            approval_id=approval_id,
            approval_type=view.approval_type,
            change_set_ref=change_set_ref,
            decided_by=view.decided_by_employee,
            decided_at=view.decided_at,
        )
        try:
            await self._compliance.activate_country_policy(
                run.tenant_id, version_id, approval, actor=self._actor
            )
        except CountryPolicyBaseVersionConflictError:
            return await self._failed(
                run, approval_id, "COUNTRY_POLICY_BASE_VERSION_CONFLICT"
            )
        except (
            CountryPolicyApprovalFactInvalidError,
            CountryPolicyActivationConflictError,
        ):
            return await self._failed(
                run, approval_id, "COUNTRY_POLICY_APPROVAL_FACT_INVALID"
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
        _, _, content_hash, _ = _identity(run)
        await self._approvals.mark_applied(
            run.tenant_id,
            _approval_id(run),
            f"country-policy-activate:{run.subject_ref}:{content_hash}",
        )
        return ("complete", None, {"application_state": "applied"})


__all__ = (
    "ApplyPolicyStep",
    "AssemblePackageStep",
    "ExpireApprovalStep",
    "MarkAppliedStep",
    "SubmitApprovalStep",
    "WaitDecisionStep",
)
