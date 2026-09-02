"""HTTP 与 scheduler 共用的单项寻源准入编排。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import (
    NeedFact,
    SourcingAdmissionReadView,
    SourcingCaseReadView,
    SourcingNeedSnapshot,
)
from domains.sourcing.service import (
    AdmissionBlockedReason,
    AdmissionState,
    SourcingAdmission,
)
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import (
    EmployeeId,
    SourcingAdmissionId,
    SourcingCaseId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType
from workflows.sourcing_case.application import (
    SourcingAdmissionApplication,
    SourcingAdmissionPolicyRead,
)

TENANT = TenantId("tenant-admission-application")
OTHER_TENANT = TenantId("tenant-other")
NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
LEASE = timedelta(minutes=5)
ADMISSION_ID = SourcingAdmissionId("sad-application")
CASE_ID = SourcingCaseId("src-application")
NEED_ID = ValidatedNeedId("need-application")
USER = SourcingActor("emp-boss", TENANT, SourcingScope.TENANT, "boss")
SOURCING_USER = SourcingActor(
    "emp-sourcing", TENANT, SourcingScope.TENANT, "sourcing"
)
SYSTEM = SourcingActor(
    "system:sourcing-admission", TENANT, SourcingScope.SYSTEM, "system"
)


def _provenance() -> ProvenanceSummary:
    return ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-application",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-customer"),
        confirmed_at=NOW,
    )


def _snapshot() -> SourcingNeedSnapshot:
    provenance = _provenance()
    return SourcingNeedSnapshot(
        need_id=NEED_ID,
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(value="Industrial Hinges", provenance=provenance),
        application=NeedFact(value="Marine Door", provenance=provenance),
        material=NeedFact(value="Stainless Steel", provenance=provenance),
        size_spec=NeedFact(value="100 mm", provenance=provenance),
        quantity=NeedFact(value=5000, provenance=provenance),
        snapshot_hash="a" * 64,
    )


def _case() -> SourcingCaseReadView:
    return SourcingCaseReadView(
        case_id=CASE_ID,
        need_id=NEED_ID,
        state="opened",
        workflow_version=2,
        version=1,
        opened_at=NOW,
        need_snapshot=_snapshot(),
    )


def _view(
    state: str = "waiting",
    *,
    blocked_reason: str | None = None,
) -> SourcingAdmissionReadView:
    return SourcingAdmissionReadView(
        admission_id=ADMISSION_ID,
        case_id=CASE_ID,
        need_id=NEED_ID,
        state=state,
        blocked_reason=blocked_reason,
        snapshot_id=(None if blocked_reason == "priority_facts_invalid" else "sps-1"),
        cluster_id=(None if blocked_reason == "priority_facts_invalid" else "ncl-1"),
        cluster_member_count=(
            None if blocked_reason == "priority_facts_invalid" else 8
        ),
        ready_at=NOW - timedelta(days=1),
        facts_observed_at=(None if blocked_reason == "priority_facts_invalid" else NOW),
        ranking_version=(
            None
            if blocked_reason == "priority_facts_invalid"
            else "need-cluster-admission-v1"
        ),
        explanation=(
            None
            if blocked_reason == "priority_facts_invalid"
            else "该需求簇当前有 8 条已验证需求；同规模需求按等待时间排序。"
        ),
        waiting_duration_seconds=86_400,
        admitted_at=NOW if state == "admitted" else None,
        admitted_by="emp-boss" if state == "admitted" else None,
        can_current_user_manual_start=state == "waiting",
    )


def _claimed() -> SourcingAdmission:
    return SourcingAdmission(
        tenant_id=TENANT,
        admission_id=ADMISSION_ID,
        case_id=CASE_ID,
        need_id=NEED_ID,
        state=AdmissionState.STARTING,
        ready_at=NOW - timedelta(days=1),
        current_snapshot_id="sps-1",  # type: ignore[arg-type]
        claim_token="manual-request-1",
        claim_expires_at=NOW + LEASE,
        workflow_run_id=None,
        blocked_reason=None,
        admitted_at=None,
        admitted_by=None,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
    )


class _Policy:
    def __init__(self, value: object = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[TenantId] = []

    async def read(self, tenant_id: TenantId) -> object:
        self.calls.append(tenant_id)
        if self.error is not None:
            raise self.error
        return self.value


class _Sourcing:
    def __init__(self, view: SourcingAdmissionReadView | None = None) -> None:
        self.view = view or _view()
        self.calls: list[tuple[object, ...]] = []
        self.case_error: Exception | None = None
        self.case_value: object = _case()

    async def list_admissions(self, tenant_id, *, state, limit, now, actor):
        self.calls.append(("list", tenant_id, state, limit, now, actor))
        return [self.view]

    async def get_admission(self, tenant_id, admission_id, *, actor):
        self.calls.append(("get", tenant_id, admission_id, actor))
        return self.view

    async def get_admission_case_snapshot(self, tenant_id, case_id, *, actor):
        self.calls.append(("case", tenant_id, case_id, actor))
        if self.case_error is not None:
            raise self.case_error
        return self.case_value

    async def claim_manual_admission(
        self, tenant_id, admission_id, command, *, claim_expires_at, actor
    ):
        self.calls.append(
            (
                "claim",
                tenant_id,
                admission_id,
                command,
                claim_expires_at,
                actor,
            )
        )
        return _claimed()


class _Starter:
    def __init__(
        self, outcome: str = "admitted", sourcing: _Sourcing | None = None
    ) -> None:
        self.outcome = outcome
        self.sourcing = sourcing
        self.calls: list[tuple[SourcingAdmission, SourcingActor]] = []

    async def admit_one(
        self,
        admission: SourcingAdmission,
        *,
        completing_actor: SourcingActor,
    ) -> str:
        self.calls.append((admission, completing_actor))
        if self.outcome == "admitted" and self.sourcing is not None:
            self.sourcing.view = _view("admitted")
        return self.outcome


def _application(
    sourcing: _Sourcing,
    policy: _Policy | None = None,
    starter: _Starter | None = None,
) -> tuple[SourcingAdmissionApplication, _Starter]:
    resolved_starter = starter or _Starter(sourcing=sourcing)
    return (
        SourcingAdmissionApplication(
            sourcing=sourcing,  # type: ignore[arg-type]
            policy=policy or _Policy(),  # type: ignore[arg-type]
            starter=resolved_starter,  # type: ignore[arg-type]
            tenant_id=TENANT,
            sourcing_actor=SYSTEM,
            lease_duration=LEASE,
            now=lambda: NOW,
        ),
        resolved_starter,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "expected_status"),
    [
        (_Policy(), "policy_not_configured"),
        (
            _Policy(SourcingAdmissionPolicyRead("dir-1", 7, False, 3)),
            "automatic_admission_disabled",
        ),
        (
            _Policy(error=RuntimeError("postgres://user:secret@example.invalid")),
            "policy_status_unknown",
        ),
    ],
)
async def test_list_projects_distinct_safe_policy_states(
    policy: _Policy, expected_status: str
) -> None:
    sourcing = _Sourcing()
    application, _ = _application(sourcing, policy)

    result = await application.list_read_view(
        TENANT, state="waiting", limit=50, actor=USER
    )

    assert result.policy.status == expected_status
    assert result.items == (_view(),)
    assert "secret" not in str(result.policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [USER, SOURCING_USER])
async def test_manual_admit_preserves_authorized_employee_as_completing_actor(
    actor: SourcingActor,
) -> None:
    sourcing = _Sourcing()
    application, starter = _application(sourcing)

    result = await application.admit_one(
        TENANT,
        ADMISSION_ID,
        request_id="manual-request-1",
        actor=actor,
    )

    assert result == _view("admitted")
    assert [call[0] for call in sourcing.calls] == ["get", "case", "claim", "get"]
    assert sourcing.calls[2][3].request_id == "manual-request-1"
    assert sourcing.calls[2][4] == NOW + LEASE
    assert starter.calls == [(_claimed(), actor)]


@pytest.mark.asyncio
async def test_manual_replay_reads_canonical_case_and_returns_admitted_view_without_start() -> (
    None
):
    sourcing = _Sourcing(_view("admitted"))
    application, starter = _application(sourcing)

    result = await application.admit_one(
        TENANT,
        ADMISSION_ID,
        request_id="manual-request-1",
        actor=USER,
    )

    assert result == _view("admitted")
    assert [call[0] for call in sourcing.calls] == ["get", "case"]
    assert starter.calls == []


@pytest.mark.asyncio
async def test_manual_rejects_blocked_invalid_admission_before_claim_or_start() -> None:
    sourcing = _Sourcing(
        _view(
            "blocked",
            blocked_reason=AdmissionBlockedReason.PRIORITY_FACTS_INVALID.value,
        )
    )
    application, starter = _application(sourcing)

    with pytest.raises(InvalidStateTransition, match="^寻源准入当前不可人工启动$"):
        await application.admit_one(
            TENANT,
            ADMISSION_ID,
            request_id="manual-request-1",
            actor=USER,
        )

    assert [call[0] for call in sourcing.calls] == ["get", "case"]
    assert starter.calls == []


@pytest.mark.asyncio
async def test_manual_unknown_start_result_is_fixed_503_and_tenant_gate_precedes_io() -> (
    None
):
    sourcing = _Sourcing()
    application, _ = _application(sourcing, starter=_Starter("pending_recovery"))

    with pytest.raises(TransientError, match="^寻源准入启动状态暂不可确认$") as caught:
        await application.admit_one(
            TENANT,
            ADMISSION_ID,
            request_id="manual-request-1",
            actor=USER,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    sourcing.calls.clear()
    wrong_actor = SourcingActor("emp-boss", OTHER_TENANT, SourcingScope.TENANT, "boss")
    with pytest.raises(PermissionDenied):
        await application.admit_one(
            TENANT,
            ADMISSION_ID,
            request_id="manual-request-1",
            actor=wrong_actor,
        )
    assert sourcing.calls == []


@pytest.mark.asyncio
async def test_manual_permanent_case_validation_claims_then_blocks_via_shared_starter() -> (
    None
):
    sourcing = _Sourcing()
    sourcing.case_error = ValidationError("private frozen snapshot mismatch")
    application, starter = _application(sourcing, starter=_Starter("blocked"))

    with pytest.raises(InvalidStateTransition, match="^寻源准入当前不可人工启动$"):
        await application.admit_one(
            TENANT,
            ADMISSION_ID,
            request_id="manual-request-1",
            actor=USER,
        )

    assert [call[0] for call in sourcing.calls] == ["get", "case", "claim"]
    assert starter.calls == [(_claimed(), USER)]


@pytest.mark.asyncio
async def test_manual_missing_canonical_case_claims_then_blocks() -> None:
    sourcing = _Sourcing()
    sourcing.case_value = None
    application, starter = _application(sourcing, starter=_Starter("blocked"))

    with pytest.raises(InvalidStateTransition, match="^寻源准入当前不可人工启动$"):
        await application.admit_one(
            TENANT,
            ADMISSION_ID,
            request_id="manual-request-1",
            actor=USER,
        )

    assert [call[0] for call in sourcing.calls] == ["get", "case", "claim"]
    assert starter.calls == [(_claimed(), USER)]
