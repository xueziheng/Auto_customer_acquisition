"""Company Playbook 变更的 durable 状态机与审批事件关联器。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from domains.approvals.service import ApprovalService
from domains.organization.permissions import OrganizationActor
from domains.organization.service import OrganizationService
from shared.errors import TransientError, ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import ApprovalDecided, DomainEvent
from shared.schemas.identifiers import ApprovalId, RunId
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
    WorkflowEngine,
)
from workflows.playbook_change.steps import (
    ApplyPlaybookStep,
    AssemblePackageStep,
    ExpireApprovalStep,
    MarkAppliedStep,
    SubmitApprovalStep,
    WaitDecisionStep,
)

PLAYBOOK_CHANGE_WORKFLOW_TYPE = "playbook_change"
_CHANGE_SET_PATTERN = re.compile(
    r"playbook:(pbv_[0-9A-HJKMNP-TV-Z]{26}):[0-9a-f]{64}\Z"
)


class OutboxHandlerRegistry(Protocol):
    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None: ...


def build_playbook_change_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=PLAYBOOK_CHANGE_WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("assemble_package", "playbook_change.assemble"),
            StepDefinition("submit_approval", "playbook_change.submit"),
            StepDefinition(
                "wait_decision",
                "playbook_change.wait",
                timeout_context_key="approval_timeout_seconds",
                on_timeout="expire_approval",
                wait_event_type="ApprovalDecided",
                run_on_entry=True,
            ),
            StepDefinition("expire_approval", "playbook_change.expire"),
            StepDefinition("apply_playbook", "playbook_change.apply"),
            StepDefinition("mark_applied", "playbook_change.mark_applied"),
        ),
        transitions={
            "assemble_package": ("submit_approval",),
            "submit_approval": ("wait_decision",),
            "wait_decision": ("apply_playbook",),
            "expire_approval": ("apply_playbook",),
            "apply_playbook": ("mark_applied",),
            "mark_applied": (),
        },
    )


def build_playbook_change_handlers(
    organization: OrganizationService,
    approvals: ApprovalService,
    system_actor: OrganizationActor,
) -> Mapping[str, StepHandler]:
    return {
        "playbook_change.assemble": AssemblePackageStep(organization, system_actor),
        "playbook_change.submit": SubmitApprovalStep(
            organization, approvals, system_actor
        ),
        "playbook_change.wait": WaitDecisionStep(approvals),
        "playbook_change.expire": ExpireApprovalStep(approvals),
        "playbook_change.apply": ApplyPlaybookStep(
            organization, approvals, system_actor
        ),
        "playbook_change.mark_applied": MarkAppliedStep(approvals),
    }


class ApprovalDecidedHandler:
    def __init__(self, engine: WorkflowEngine, approvals: ApprovalService) -> None:
        self._engine = engine
        self._approvals = approvals

    async def handle(self, event: ApprovalDecided) -> None:
        approval_id = ApprovalId(str(event.approval_id))
        view = await self._approvals.get(event.tenant_id, approval_id)
        if view.approval_type != "playbook_change":
            return
        if view.approval_id != str(approval_id):
            raise ValidationError("Playbook 审批事件与审批记录不匹配")
        match = _CHANGE_SET_PATTERN.fullmatch(view.change_set_ref or "")
        if match is None:
            raise ValidationError("Playbook 审批变更集引用无效")
        if event.decision not in {"approve", "reject"}:
            raise ValidationError("Playbook 审批事件决定无效")
        version_id = match.group(1)
        payload = {
            "approval_id": str(event.approval_id),
            "decision": event.decision,
            "decided_by": (None if event.decided_by is None else str(event.decided_by)),
        }
        run_id: RunId | None = await self._engine.find_active_run(
            event.tenant_id, PLAYBOOK_CHANGE_WORKFLOW_TYPE, version_id
        )
        if run_id is None:
            if await self._engine.has_delivered_event(
                event.tenant_id,
                PLAYBOOK_CHANGE_WORKFLOW_TYPE,
                version_id,
                "ApprovalDecided",
                payload,
            ):
                return
            raise TransientError("Playbook 变更活动流程暂不可用")
        accepted = await self._engine.deliver_event(
            event.tenant_id, run_id, "ApprovalDecided", payload
        )
        if not accepted:
            raise TransientError("Playbook 变更活动流程暂不可用")


def register_playbook_change(
    engine: WorkflowEngine,
    registry: OutboxHandlerRegistry,
    approvals: ApprovalService,
) -> None:
    engine.register(build_playbook_change_definition())
    registry.register_handler(
        ApprovalDecided,
        "playbook_change.approval_decided",
        ApprovalDecidedHandler(engine, approvals),  # type: ignore[arg-type]
    )


__all__ = (
    "build_playbook_change_definition",
    "build_playbook_change_handlers",
    "register_playbook_change",
)
