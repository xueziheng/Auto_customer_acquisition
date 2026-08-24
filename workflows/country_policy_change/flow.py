"""国家政策包变更的 durable 状态机与安全事件关联。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from domains.approvals.service import ApprovalService
from domains.compliance.permissions import ComplianceActor
from domains.compliance.schemas import normalize_country_key
from domains.compliance.service import ComplianceService
from shared.errors import TransientError, ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import (
    ApprovalDecided,
    CountryPolicyVersionProposed,
    DomainEvent,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyVersionId,
    RunId,
    TenantId,
)
from workflows.country_policy_change.steps import (
    ApplyPolicyStep,
    AssemblePackageStep,
    ExpireApprovalStep,
    MarkAppliedStep,
    SubmitApprovalStep,
    WaitDecisionStep,
)
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
    WorkflowEngine,
)

COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE = "country_policy_change"
_CHANGE_SET_PATTERN = re.compile(
    r"country_policy:(cpp_[0-9A-HJKMNP-TV-Z]{26}):[0-9a-f]{64}\Z"
)
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class OutboxHandlerRegistry(Protocol):
    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None: ...


def country_policy_change_idempotency_key(
    tenant_id: TenantId, version_id: CountryPolicyVersionId
) -> str:
    """生成 API 与 outbox recovery 共用的稳定 workflow 幂等键。"""
    return f"country-policy-change:{tenant_id}:{version_id}"


def build_country_policy_change_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("assemble_package", "country_policy_change.assemble"),
            StepDefinition("submit_approval", "country_policy_change.submit"),
            StepDefinition(
                "wait_decision",
                "country_policy_change.wait",
                timeout_context_key="approval_timeout_seconds",
                on_timeout="expire_approval",
                wait_event_type="ApprovalDecided",
                run_on_entry=True,
            ),
            StepDefinition("expire_approval", "country_policy_change.expire"),
            StepDefinition("apply_policy", "country_policy_change.apply"),
            StepDefinition("mark_applied", "country_policy_change.mark_applied"),
        ),
        transitions={
            "assemble_package": ("submit_approval",),
            "submit_approval": ("wait_decision",),
            "wait_decision": ("apply_policy",),
            "expire_approval": ("apply_policy",),
            "apply_policy": ("mark_applied",),
            "mark_applied": (),
        },
    )


def build_country_policy_change_handlers(
    compliance: ComplianceService,
    approvals: ApprovalService,
    system_actor: ComplianceActor,
) -> Mapping[str, StepHandler]:
    return {
        "country_policy_change.assemble": AssemblePackageStep(compliance, system_actor),
        "country_policy_change.submit": SubmitApprovalStep(
            compliance, approvals, system_actor
        ),
        "country_policy_change.wait": WaitDecisionStep(approvals),
        "country_policy_change.expire": ExpireApprovalStep(approvals),
        "country_policy_change.apply": ApplyPolicyStep(
            compliance, approvals, system_actor
        ),
        "country_policy_change.mark_applied": MarkAppliedStep(approvals),
    }


class CountryPolicyVersionProposedHandler:
    """用持久 proposal event 修复 commit/start 崩溃窗口。"""

    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def handle(self, event: CountryPolicyVersionProposed) -> RunId:
        version_id = CountryPolicyVersionId(str(event.country_policy_version_id))
        content_hash = str(event.content_hash)
        country_key = str(event.country_key)
        proposed_by = str(event.proposed_by)
        if _HASH_PATTERN.fullmatch(content_hash) is None:
            raise ValidationError("国家政策候选事件内容哈希无效")
        try:
            normalized_country_key = normalize_country_key(country_key)
        except ValueError as exc:
            raise ValidationError("国家政策候选事件国家键无效") from exc
        if country_key != normalized_country_key:
            raise ValidationError("国家政策候选事件国家键无效")
        if not proposed_by or proposed_by != proposed_by.strip():
            raise ValidationError("国家政策候选事件提交人无效")
        change_set_ref = f"country_policy:{version_id}:{content_hash}"
        return await self._engine.start(
            event.tenant_id,
            COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE,
            str(version_id),
            {
                "country_policy_version_id": str(version_id),
                "country_key": country_key,
                "content_hash": content_hash,
                "change_set_ref": change_set_ref,
                "proposed_by": proposed_by,
            },
            country_policy_change_idempotency_key(event.tenant_id, version_id),
        )


class ApprovalDecidedHandler:
    """只把精确国家政策审批事件投给对应活动 run。"""

    def __init__(self, engine: WorkflowEngine, approvals: ApprovalService) -> None:
        self._engine = engine
        self._approvals = approvals

    async def handle(self, event: ApprovalDecided) -> None:
        approval_id = ApprovalId(str(event.approval_id))
        view = await self._approvals.get(event.tenant_id, approval_id)
        if view.approval_type != COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE:
            return
        if view.approval_id != str(approval_id):
            raise ValidationError("国家政策审批事件与审批记录不匹配")
        match = _CHANGE_SET_PATTERN.fullmatch(view.change_set_ref or "")
        if match is None:
            raise ValidationError("国家政策审批变更集引用无效")
        if event.decision not in {"approve", "reject"}:
            raise ValidationError("国家政策审批事件决定无效")
        version_id = match.group(1)
        payload = {
            "approval_id": str(event.approval_id),
            "decision": event.decision,
            "decided_by": (None if event.decided_by is None else str(event.decided_by)),
        }
        run_id = await self._engine.find_active_run(
            event.tenant_id, COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE, version_id
        )
        if run_id is None:
            if await self._engine.has_delivered_event(
                event.tenant_id,
                COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE,
                version_id,
                "ApprovalDecided",
                payload,
            ):
                return
            raise TransientError("国家政策变更活动流程暂不可用")
        accepted = await self._engine.deliver_event(
            event.tenant_id, run_id, "ApprovalDecided", payload
        )
        if not accepted:
            raise TransientError("国家政策变更活动流程暂不可用")


def register_country_policy_change(
    engine: WorkflowEngine,
    registry: OutboxHandlerRegistry,
    approvals: ApprovalService,
) -> None:
    engine.register(build_country_policy_change_definition())
    registry.register_handler(
        CountryPolicyVersionProposed,
        "country_policy_change.version_proposed",
        CountryPolicyVersionProposedHandler(engine),  # type: ignore[arg-type]
    )
    registry.register_handler(
        ApprovalDecided,
        "country_policy_change.approval_decided",
        ApprovalDecidedHandler(engine, approvals),  # type: ignore[arg-type]
    )


__all__ = (
    "ApprovalDecidedHandler",
    "CountryPolicyVersionProposedHandler",
    "build_country_policy_change_definition",
    "build_country_policy_change_handlers",
    "country_policy_change_idempotency_key",
    "register_country_policy_change",
)
