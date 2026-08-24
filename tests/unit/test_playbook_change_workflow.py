"""Company Playbook 审批工作流的定义、关联、超时与重放安全。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from domains.approvals.schemas import ApprovalView
from domains.organization.errors import (
    PlaybookActivationConflictError,
    PlaybookApprovalFactInvalidError,
    PlaybookBaseVersionConflictError,
)
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
)
from domains.organization.schemas import PlaybookChangeSnapshot, PlaybookVersionView
from shared.errors import TransientError, ValidationError
from shared.events.catalog import ApprovalDecided
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    PlaybookVersionId,
    RunId,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.playbook_change import (
    build_playbook_change_definition,
    build_playbook_change_handlers,
    register_playbook_change,
)
from workflows.playbook_change.steps import ApplyPlaybookStep

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
TENANT = TenantId("tenant-playbook-workflow")
VERSION_ID = PlaybookVersionId("pbv_01K00000000000000000000000")
APPROVAL_ID = ApprovalId("apr_01K00000000000000000000000")
PROPOSER = EmployeeId("emp_01K00000000000000000000000")
APPROVER = EmployeeId("emp_01K00000000000000000000001")
CONTENT_HASH = "a" * 64
CHANGE_SET_REF = f"playbook:{VERSION_ID}:{CONTENT_HASH}"


def _system_actor() -> OrganizationActor:
    return OrganizationActor(
        "system:playbook-change",
        OrganizationScope(OrganizationScopeLevel.SYSTEM, TENANT),
        "system",
    )


def _version() -> PlaybookVersionView:
    return PlaybookVersionView(
        playbook_version_id=VERSION_ID,
        version_number=1,
        content_hash=CONTENT_HASH,
        base_version_id=None,
        base_content_hash=None,
        company_type="trading_company",
        minimum_deal_amount="10000.00",
        minimum_deal_currency="USD",
        excluded_categories=("adult",),
        sourcing_regions=("guangdong",),
        excluded_countries=("north korea",),
        monthly_budget_credits=500,
        approval_requirements=("catalog_reference_price",),
        supply_capabilities_note="Hardware sourcing.",
        proposed_by=PROPOSER,
        proposed_at=NOW,
        content_provenance=Provenance(
            source_type=SourceType.EMPLOYEE_INPUT,
            source_id="settings-form",
            extracted_by="human",
            extracted_at=NOW,
            confirmed_by=PROPOSER,
            confirmed_at=NOW,
        ),
        change_set_ref=CHANGE_SET_REF,
    )


def _snapshot(*, base_is_current: bool = True) -> PlaybookChangeSnapshot:
    return PlaybookChangeSnapshot(
        base=None,
        current=None,
        candidate=_version(),
        base_is_current=base_is_current,
    )


def _view(state: str = "approved", **overrides: object) -> ApprovalView:
    values: dict[str, object] = {
        "approval_id": str(APPROVAL_ID),
        "approval_type": "playbook_change",
        "type_label": "Company Playbook 变更",
        "title": "激活 Company Playbook v1",
        "reason": "老板提交了新的公司经营边界。",
        "proposed_change_display": {"before": "未配置", "after": "候选版本"},
        "affected_entities": [f"Company Playbook {VERSION_ID}"],
        "if_approved": "激活精确候选版本",
        "if_rejected": "保持当前版本",
        "reversible": True,
        "state": state,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "change_set_ref": CHANGE_SET_REF,
        "decided_by_employee": APPROVER if state == "approved" else None,
        "decided_at": NOW + timedelta(minutes=1) if state == "approved" else None,
    }
    values.update(overrides)
    return ApprovalView(**values)  # type: ignore[arg-type]


def _run(
    step: str, *, state: str | None = None, event: dict[str, Any] | None = None
) -> WorkflowRun:
    context: dict[str, Any] = {
        "playbook_version_id": str(VERSION_ID),
        "content_hash": CONTENT_HASH,
        "change_set_ref": CHANGE_SET_REF,
        "proposed_by": str(PROPOSER),
    }
    if step != "assemble_package":
        context.update(approval_id=str(APPROVAL_ID), approval_timeout_seconds=604800)
    if state is not None:
        context["application_state"] = state
    if event is not None:
        context["event"] = event
    return WorkflowRun(
        run_id=RunId("run_01K00000000000000000000000"),
        tenant_id=TENANT,
        workflow_type="playbook_change",
        workflow_version=1,
        subject_ref=str(VERSION_ID),
        current_step=step,
        status=StepStatus.RUNNING,
        created_at=NOW,
        context=context,
    )


class FakeApprovals:
    def __init__(self, view: ApprovalView | None = None) -> None:
        self.view = view or _view()
        self.submissions: list[dict[str, Any]] = []
        self.expired = 0
        self.applied: list[tuple[ApprovalId, str]] = []
        self.failed: list[tuple[ApprovalId, str]] = []

    async def submit(
        self,
        tenant_id,
        approval_type,
        title,
        proposed_change,
        reason,
        blast_radius,
        **kwargs,
    ):
        self.submissions.append(
            {
                "tenant_id": tenant_id,
                "approval_type": approval_type,
                "title": title,
                "proposed_change": proposed_change,
                "reason": reason,
                "blast_radius": blast_radius,
                **kwargs,
            }
        )
        return APPROVAL_ID

    async def get(self, tenant_id, approval_id, *, current_employee=None):
        return self.view

    async def expire_overdue(self, tenant_id):
        self.expired += 1
        return 1

    async def mark_applied(self, tenant_id, approval_id, idempotency_key):
        self.applied.append((approval_id, idempotency_key))
        return len(self.applied) == 1

    async def mark_apply_failed(self, tenant_id, approval_id, error):
        self.failed.append((approval_id, error))


class FakeOrganization:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.activations: list[tuple[Any, ...]] = []

    async def get_change_snapshot(self, tenant_id, version_id, *, actor):
        return _snapshot()

    async def activate_playbook(self, tenant_id, version_id, approval, *, actor):
        if self.error is not None:
            raise self.error
        self.activations.append((tenant_id, version_id, approval, actor))
        return object()


def test_playbook_change_definition_waits_for_approval_event() -> None:
    definition = build_playbook_change_definition()
    assert [step.step_name for step in definition.steps] == [
        "assemble_package",
        "submit_approval",
        "wait_decision",
        "expire_approval",
        "apply_playbook",
        "mark_applied",
    ]
    wait = next(step for step in definition.steps if step.step_name == "wait_decision")
    assert wait.wait_event_type == "ApprovalDecided"
    assert wait.timeout_context_key == "approval_timeout_seconds"
    assert wait.on_timeout == "expire_approval"
    assert wait.run_on_entry is True
    assert definition.transitions == {
        "assemble_package": ("submit_approval",),
        "submit_approval": ("wait_decision",),
        "wait_decision": ("apply_playbook",),
        "expire_approval": ("apply_playbook",),
        "apply_playbook": ("mark_applied",),
        "mark_applied": (),
    }


@pytest.mark.asyncio
async def test_assemble_and_submit_persist_only_ids_hash_and_state() -> None:
    approvals = FakeApprovals(_view("pending"))
    organization = FakeOrganization()
    handlers = build_playbook_change_handlers(organization, approvals, _system_actor())
    assembled = await handlers["playbook_change.assemble"].execute(
        _run("assemble_package")
    )
    submitted = await handlers["playbook_change.submit"].execute(
        _run("submit_approval")
    )

    assert assembled == (
        "advance",
        "submit_approval",
        {
            "playbook_version_id": str(VERSION_ID),
            "content_hash": CONTENT_HASH,
            "change_set_ref": CHANGE_SET_REF,
            "proposed_by": str(PROPOSER),
        },
    )
    assert submitted[0:2] == ("advance", "wait_decision")
    assert submitted[2] == {
        "approval_id": str(APPROVAL_ID),
        "approval_timeout_seconds": 604800,
        "change_set_ref": CHANGE_SET_REF,
    }
    assert approvals.submissions[0]["proposed_change"]["before"] == "未配置"
    assert (
        approvals.submissions[0]["proposed_change"]["after"]["minimum_deal_amount"]
        == "10000.00"
    )
    rendered = repr((assembled[2], submitted[2])).casefold()
    assert not any(
        key in rendered
        for key in ("minimum_deal", "password", "secret", "token", "cookie")
    )


@pytest.mark.asyncio
async def test_submit_warns_when_captured_base_is_no_longer_current() -> None:
    class StaleOrganization(FakeOrganization):
        async def get_change_snapshot(self, tenant_id, version_id, *, actor):
            return _snapshot(base_is_current=False)

    approvals = FakeApprovals(_view("pending"))
    handlers = build_playbook_change_handlers(
        StaleOrganization(), approvals, _system_actor()
    )

    await handlers["playbook_change.submit"].execute(_run("submit_approval"))

    assert (
        "候选基准已变化，批准后将进入 apply_failed，需基于最新版本重新提交"
        in approvals.submissions[0]["reason"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["rejected", "expired"])
async def test_wait_step_completes_rejected_or_expired_without_activation(
    state: str,
) -> None:
    approvals = FakeApprovals(_view(state))
    handlers = build_playbook_change_handlers(
        FakeOrganization(), approvals, _system_actor()
    )
    event = {
        "event_type": "ApprovalDecided",
        "payload": {"approval_id": str(APPROVAL_ID), "decision": state},
    }
    assert await handlers["playbook_change.wait"].execute(
        _run("wait_decision", event=event)
    ) == ("complete", None, {"approval_state": state})


@pytest.mark.asyncio
async def test_wait_step_reloads_record_and_rejects_wrong_event_correlation() -> None:
    handlers = build_playbook_change_handlers(
        FakeOrganization(), FakeApprovals(), _system_actor()
    )
    event = {
        "event_type": "ApprovalDecided",
        "payload": {"approval_id": "apr_wrong", "decision": "approve"},
    }
    with pytest.raises(ValidationError):
        await handlers["playbook_change.wait"].execute(
            _run("wait_decision", event=event)
        )


@pytest.mark.asyncio
async def test_wait_entry_without_event_reloads_pending_fact_and_waits() -> None:
    handlers = build_playbook_change_handlers(
        FakeOrganization(), FakeApprovals(_view("pending")), _system_actor()
    )

    assert await handlers["playbook_change.wait"].execute(_run("wait_decision")) == (
        "wait",
        None,
        {"approval_state": "pending"},
    )


@pytest.mark.asyncio
async def test_expire_step_never_defaults_pending_to_approval() -> None:
    approvals = FakeApprovals(_view("pending"))
    handlers = build_playbook_change_handlers(
        FakeOrganization(), approvals, _system_actor()
    )
    with pytest.raises(TransientError):
        await handlers["playbook_change.expire"].execute(_run("expire_approval"))
    assert approvals.expired == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PlaybookBaseVersionConflictError("stale"), "PLAYBOOK_BASE_VERSION_CONFLICT"),
        (
            PlaybookApprovalFactInvalidError("bad fact"),
            "PLAYBOOK_APPROVAL_FACT_INVALID",
        ),
        (
            PlaybookActivationConflictError("bad replay"),
            "PLAYBOOK_APPROVAL_FACT_INVALID",
        ),
    ],
)
async def test_apply_step_marks_deterministic_candidate_failure(
    error: BaseException, code: str
) -> None:
    approvals = FakeApprovals(_view())
    step = ApplyPlaybookStep(FakeOrganization(error=error), approvals, _system_actor())

    assert await step.execute(_run("apply_playbook")) == (
        "complete",
        None,
        {"application_state": "apply_failed", "application_error_code": code},
    )
    assert approvals.failed == [(APPROVAL_ID, code)]


@pytest.mark.asyncio
async def test_activate_commits_before_mark_applied_and_crash_replay_is_safe() -> None:
    approvals = FakeApprovals(_view())
    organization = FakeOrganization()
    handlers = build_playbook_change_handlers(organization, approvals, _system_actor())
    assert await handlers["playbook_change.apply"].execute(_run("apply_playbook")) == (
        "advance",
        "mark_applied",
        {"application_state": "activated"},
    )
    assert approvals.applied == []

    expected_key = f"playbook-activate:{VERSION_ID}:{CONTENT_HASH}"
    assert await handlers["playbook_change.mark_applied"].execute(
        _run("mark_applied")
    ) == ("complete", None, {"application_state": "applied"})
    assert approvals.applied == [(APPROVAL_ID, expected_key)]


@pytest.mark.asyncio
async def test_apply_step_propagates_infrastructure_failure_for_retry() -> None:
    approvals = FakeApprovals(_view())
    step = ApplyPlaybookStep(
        FakeOrganization(error=ConnectionError("database unavailable")),
        approvals,
        _system_actor(),
    )

    with pytest.raises(ConnectionError):
        await step.execute(_run("apply_playbook"))
    assert approvals.failed == []


class FakeEngine:
    def __init__(self) -> None:
        self.registered: list[Any] = []
        self.active: RunId | None = RunId("run_01K00000000000000000000000")
        self.deliveries: list[tuple[Any, ...]] = []
        self.delivered = False

    def register(self, definition) -> None:
        self.registered.append(definition)

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        return self.active

    async def deliver_event(self, tenant_id, run_id, event_type, payload):
        self.deliveries.append((tenant_id, run_id, event_type, payload))
        self.active = None
        self.delivered = True
        return True

    async def has_delivered_event(
        self, tenant_id, workflow_type, subject_ref, event_type, payload
    ):
        return self.delivered


class FakeRegistry:
    def __init__(self) -> None:
        self.rows: list[tuple[type, str, Any]] = []

    def register_handler(self, event_type, handler_name, handler) -> None:
        self.rows.append((event_type, handler_name, handler))


@pytest.mark.asyncio
async def test_approval_event_handler_correlates_exact_change_set_and_replay() -> None:
    engine = FakeEngine()
    registry = FakeRegistry()
    approvals = FakeApprovals(_view())
    register_playbook_change(engine, registry, approvals)
    handler = registry.rows[0][2]
    event = ApprovalDecided(
        tenant_id=TENANT,
        occurred_at=NOW,
        approval_id=str(APPROVAL_ID),
        decision="approve",
        decided_by=APPROVER,
    )

    await handler.handle(event)
    await handler.handle(event)

    assert len(engine.deliveries) == 1
    assert engine.deliveries[0][3] == {
        "approval_id": str(APPROVAL_ID),
        "decision": "approve",
        "decided_by": str(APPROVER),
    }


@pytest.mark.asyncio
async def test_event_handler_ignores_other_approval_types_and_rejects_bad_change_set() -> (
    None
):
    engine = FakeEngine()
    registry = FakeRegistry()
    approvals = FakeApprovals(_view(approval_type="quote_send"))
    register_playbook_change(engine, registry, approvals)
    event = ApprovalDecided(TENANT, NOW, None, str(APPROVAL_ID), "approve", APPROVER)
    await registry.rows[0][2].handle(event)
    assert engine.deliveries == []

    approvals.view = _view(change_set_ref="playbook:pbv_bad:not-a-hash")
    with pytest.raises(ValidationError):
        await registry.rows[0][2].handle(event)
