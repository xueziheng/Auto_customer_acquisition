"""国家政策审批工作流的安全审批包、事件关联与稳定失败映射。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from domains.approvals.schemas import ApprovalView
from domains.approvals.service import ApprovalType
from domains.compliance.errors import (
    CountryPolicyActivationConflictError,
    CountryPolicyApprovalFactInvalidError,
    CountryPolicyBaseVersionConflictError,
)
from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyChangeSnapshot,
    CountryPolicyField,
    CountryPolicyVersionView,
)
from shared.errors import ValidationError
from shared.events.catalog import ApprovalDecided, CountryPolicyVersionProposed
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyVersionId,
    EmployeeId,
    RunId,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType
from workflows.engine.runner import StepStatus, WorkflowRun

try:
    from workflows.country_policy_change import (
        ApprovalDecidedHandler,
        CountryPolicyVersionProposedHandler,
        build_country_policy_change_definition,
        build_country_policy_change_handlers,
    )
except ModuleNotFoundError:

    def _missing(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        pytest.fail("RED：country_policy_change workflow 尚未实现")

    ApprovalDecidedHandler = _missing
    CountryPolicyVersionProposedHandler = _missing
    build_country_policy_change_definition = _missing
    build_country_policy_change_handlers = _missing

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
TENANT = TenantId("tenant-country-policy-workflow")
VERSION_ID = CountryPolicyVersionId("cpp_01K00000000000000000000000")
APPROVAL_ID = ApprovalId("apr_01K00000000000000000000000")
PROPOSER = EmployeeId("emp_01K00000000000000000000000")
APPROVER = EmployeeId("emp_01K00000000000000000000001")
CONTENT_HASH = "a" * 64
CHANGE_SET_REF = f"country_policy:{VERSION_ID}:{CONTENT_HASH}"


def _system_actor() -> ComplianceActor:
    return ComplianceActor(
        actor_id="system:country-policy-change",
        tenant_id=TENANT,
        scope=ComplianceScope.SYSTEM,
        role="system",
    )


def _provenance(field: CountryPolicyField) -> Provenance:
    return Provenance(
        source_type=SourceType.EMPLOYEE_INPUT,
        source_id=f"assessment:{field.value}",
        extracted_by=f"human:{PROPOSER}",
        extracted_at=NOW,
        confirmed_by=PROPOSER,
        confirmed_at=NOW,
    )


def _version(
    *,
    version_id: CountryPolicyVersionId = VERSION_ID,
    content_hash: str = CONTENT_HASH,
    public_research_allowed: bool = True,
) -> CountryPolicyVersionView:
    return CountryPolicyVersionView(
        country_policy_version_id=version_id,
        country="Synthetic Market",
        country_key="synthetic market",
        version_number=1,
        content_hash=content_hash,
        base_version_id=None,
        base_content_hash=None,
        public_research_allowed=public_research_allowed,
        contact_enrichment_allowed=False,
        cold_b2b_email_allowed=False,
        personal_data_basis_required=True,
        subject_type_affects_judgment=True,
        contact_type_affects_judgment=True,
        opt_out_deadline_days=30,
        local_representative_required=False,
        requirements=("honor_opt_out",),
        notes="Synthetic policy fixture.",
        field_provenance={field: _provenance(field) for field in DECISION_FIELDS},
        proposed_by=PROPOSER,
        proposed_at=NOW,
        change_set_ref=f"country_policy:{version_id}:{content_hash}",
    )


def _snapshot() -> CountryPolicyChangeSnapshot:
    base = _version(public_research_allowed=False)
    return CountryPolicyChangeSnapshot(
        base=base,
        current=base,
        candidate=_version(),
        base_is_current=True,
    )


def _view(
    state: str = "approved",
    *,
    approval_type: str = "country_policy_change",
    change_set_ref: str = CHANGE_SET_REF,
) -> ApprovalView:
    return ApprovalView(
        approval_id=str(APPROVAL_ID),
        approval_type=approval_type,
        type_label="国家政策包变更",
        title="激活国家政策包",
        reason="已提交人工核验的国家政策候选。",
        proposed_change_display={"before": "old", "after": "new"},
        affected_entities=[str(VERSION_ID)],
        if_approved="激活精确候选版本",
        if_rejected="保持当前版本",
        reversible=True,
        state=state,
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
        change_set_ref=change_set_ref,
        decided_by_employee=APPROVER if state == "approved" else None,
        decided_at=NOW + timedelta(minutes=1) if state == "approved" else None,
    )


def _run(step: str, *, event: dict[str, Any] | None = None) -> WorkflowRun:
    context: dict[str, Any] = {
        "country_policy_version_id": str(VERSION_ID),
        "country_key": "synthetic market",
        "content_hash": CONTENT_HASH,
        "change_set_ref": CHANGE_SET_REF,
        "proposed_by": str(PROPOSER),
    }
    if step != "assemble_package":
        context.update(
            approval_id=str(APPROVAL_ID), approval_timeout_seconds=7 * 24 * 60 * 60
        )
    if event is not None:
        context["event"] = event
    return WorkflowRun(
        run_id=RunId("run_01K00000000000000000000000"),
        tenant_id=TENANT,
        workflow_type="country_policy_change",
        workflow_version=1,
        subject_ref=str(VERSION_ID),
        current_step=step,
        status=StepStatus.RUNNING,
        created_at=NOW,
        context=context,
    )


class FakeCompliance:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.activations: list[tuple[Any, ...]] = []

    async def get_change_snapshot(self, tenant_id, version_id, *, actor):
        return _snapshot()

    async def activate_country_policy(self, tenant_id, version_id, approval, *, actor):
        if self.error is not None:
            raise self.error
        self.activations.append((tenant_id, version_id, approval, actor))
        return object()


class FakeApprovals:
    def __init__(self, view: ApprovalView | None = None) -> None:
        self.view = view or _view()
        self.submissions: list[dict[str, Any]] = []
        self.failed: list[tuple[ApprovalId, str]] = []
        self.applied: list[tuple[ApprovalId, str]] = []

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
        return 1

    async def mark_apply_failed(self, tenant_id, approval_id, error):
        self.failed.append((approval_id, error))

    async def mark_applied(self, tenant_id, approval_id, idempotency_key):
        self.applied.append((approval_id, idempotency_key))
        return len(self.applied) == 1


class FakeEngine:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.run_id = RunId("run_01K00000000000000000000000")
        self.active: RunId | None = self.run_id
        self.deliveries: list[tuple[Any, ...]] = []

    async def start(
        self,
        tenant_id,
        workflow_type,
        subject_ref,
        initial_context,
        idempotency_key,
        **kwargs,
    ):
        self.starts.append(
            {
                "tenant_id": tenant_id,
                "workflow_type": workflow_type,
                "subject_ref": subject_ref,
                "initial_context": initial_context,
                "idempotency_key": idempotency_key,
                **kwargs,
            }
        )
        return self.run_id

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        return self.active

    async def deliver_event(self, tenant_id, run_id, event_type, payload):
        self.deliveries.append((tenant_id, run_id, event_type, payload))
        return True

    async def has_delivered_event(self, *args, **kwargs):
        return False


def test_country_policy_change_definition_has_exact_approval_paths() -> None:
    definition = build_country_policy_change_definition()
    assert definition.workflow_type == "country_policy_change"
    assert definition.version == 1
    assert [step.step_name for step in definition.steps] == [
        "assemble_package",
        "submit_approval",
        "wait_decision",
        "expire_approval",
        "apply_policy",
        "mark_applied",
    ]
    wait = definition.steps[2]
    assert wait.wait_event_type == "ApprovalDecided"
    assert wait.timeout_context_key == "approval_timeout_seconds"
    assert wait.on_timeout == "expire_approval"
    assert wait.run_on_entry is True
    assert definition.transitions == {
        "assemble_package": ("submit_approval",),
        "submit_approval": ("wait_decision",),
        "wait_decision": ("apply_policy",),
        "expire_approval": ("apply_policy",),
        "apply_policy": ("mark_applied",),
        "mark_applied": (),
    }


@pytest.mark.asyncio
async def test_package_contains_before_after_field_diff_and_safe_provenance_refs() -> (
    None
):
    approvals = FakeApprovals(_view("pending"))
    handlers = build_country_policy_change_handlers(
        FakeCompliance(), approvals, _system_actor()
    )

    await handlers["country_policy_change.submit"].execute(_run("submit_approval"))

    package = approvals.submissions[0]["proposed_change"]
    assert package["before"]["public_research_allowed"] is False
    assert package["after"]["public_research_allowed"] is True
    assert package["field_sources"]["public_research_allowed"] == {
        "source_id": "assessment:public_research_allowed",
        "source_type": "employee_input",
    }
    rendered = repr(package).casefold()
    assert "source page body" not in rendered
    assert "<html" not in rendered


@pytest.mark.asyncio
async def test_submit_uses_country_policy_change_and_proposer_as_owner() -> None:
    approvals = FakeApprovals(_view("pending"))
    handlers = build_country_policy_change_handlers(
        FakeCompliance(), approvals, _system_actor()
    )

    result = await handlers["country_policy_change.submit"].execute(
        _run("submit_approval")
    )

    submitted = approvals.submissions[0]
    assert submitted["approval_type"] is ApprovalType.COUNTRY_POLICY_CHANGE
    assert submitted["proposed_by_employee"] == PROPOSER
    assert submitted["owner_employee"] == PROPOSER
    assert submitted["change_set_ref"] == CHANGE_SET_REF
    assert result[2]["approval_timeout_seconds"] == 604800


@pytest.mark.asyncio
async def test_approval_event_routes_only_exact_change_set_to_exact_active_run() -> (
    None
):
    engine = FakeEngine()
    approvals = FakeApprovals(_view(approval_type="playbook_change"))
    handler = ApprovalDecidedHandler(engine, approvals)
    event = ApprovalDecided(TENANT, NOW, None, str(APPROVAL_ID), "approve", APPROVER)
    await handler.handle(event)
    assert engine.deliveries == []

    approvals.view = _view(change_set_ref="country_policy:cpp_wrong:" + "b" * 64)
    with pytest.raises(ValidationError):
        await handler.handle(event)

    approvals.view = _view()
    await handler.handle(event)
    assert engine.deliveries == [
        (
            TENANT,
            engine.run_id,
            "ApprovalDecided",
            {
                "approval_id": str(APPROVAL_ID),
                "decision": "approve",
                "decided_by": str(APPROVER),
            },
        )
    ]


@pytest.mark.asyncio
async def test_apply_maps_stale_base_to_country_policy_base_version_conflict() -> None:
    approvals = FakeApprovals()
    handlers = build_country_policy_change_handlers(
        FakeCompliance(error=CountryPolicyBaseVersionConflictError("stale")),
        approvals,
        _system_actor(),
    )

    result = await handlers["country_policy_change.apply"].execute(_run("apply_policy"))

    assert result == (
        "complete",
        None,
        {
            "application_state": "apply_failed",
            "application_error_code": "COUNTRY_POLICY_BASE_VERSION_CONFLICT",
        },
    )
    assert approvals.failed == [(APPROVAL_ID, "COUNTRY_POLICY_BASE_VERSION_CONFLICT")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        CountryPolicyApprovalFactInvalidError("invalid"),
        CountryPolicyActivationConflictError("conflict"),
    ],
)
async def test_apply_maps_invalid_fact_to_country_policy_approval_fact_invalid(
    error: BaseException,
) -> None:
    approvals = FakeApprovals()
    handlers = build_country_policy_change_handlers(
        FakeCompliance(error=error), approvals, _system_actor()
    )

    result = await handlers["country_policy_change.apply"].execute(_run("apply_policy"))

    assert result[2] == {
        "application_state": "apply_failed",
        "application_error_code": "COUNTRY_POLICY_APPROVAL_FACT_INVALID",
    }
    assert approvals.failed == [(APPROVAL_ID, "COUNTRY_POLICY_APPROVAL_FACT_INVALID")]


@pytest.mark.asyncio
async def test_proposal_handler_uses_safe_context_and_deterministic_key() -> None:
    engine = FakeEngine()
    event = CountryPolicyVersionProposed(
        tenant_id=TENANT,
        occurred_at=NOW,
        country_policy_version_id=VERSION_ID,
        country_key="synthetic market",
        content_hash=CONTENT_HASH,
        proposed_by=PROPOSER,
    )

    result = await CountryPolicyVersionProposedHandler(engine).handle(event)

    assert result == engine.run_id
    assert engine.starts == [
        {
            "tenant_id": TENANT,
            "workflow_type": "country_policy_change",
            "subject_ref": str(VERSION_ID),
            "initial_context": {
                "country_policy_version_id": str(VERSION_ID),
                "country_key": "synthetic market",
                "content_hash": CONTENT_HASH,
                "change_set_ref": CHANGE_SET_REF,
                "proposed_by": str(PROPOSER),
            },
            "idempotency_key": f"country-policy-change:{TENANT}:{VERSION_ID}",
        }
    ]
