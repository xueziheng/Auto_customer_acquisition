"""老板策略门禁后的寻源准入 driver 与保守恢复语义。"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from apps.scheduler_worker._sourcing_context import _safe_context
from apps.scheduler_worker.directive_reader import (
    DirectiveSourcingAdmissionPolicyReader,
    SourcingAdmissionPolicyRead,
)
from apps.scheduler_worker.sourcing_admission import SourcingAdmissionDriver
from domains.directives.schemas import DirectiveView
from domains.directives.service import DirectiveService
from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import (
    NeedFact,
    SourcingCaseReadView,
    SourcingNeedSnapshot,
)
from domains.sourcing.service import (
    AdmissionBlockedReason,
    AdmissionState,
    SourcingAdmission,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    RunId,
    SourcingAdmissionId,
    SourcingCaseId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

TENANT = TenantId("tenant-admission-driver")
NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
LEASE = timedelta(minutes=5)
SYSTEM = SourcingActor(
    "system:sourcing-admission", TENANT, SourcingScope.SYSTEM, "system"
)


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


def _directive(
    *,
    mode: object = "cluster_ranked",
    enabled: object = True,
    batch_limit: object = 3,
) -> DirectiveView:
    return DirectiveView(
        directive_id="dir-active",
        source_proposal_id="dpr-confirmed",
        version=7,
        objective="focus_existing_needs",
        activated_at=NOW,
        activated_by_name="Boss",
        sourcing_admission_mode=mode,  # type: ignore[arg-type]
        automatic_sourcing_admission_enabled=enabled,  # type: ignore[arg-type]
        sourcing_admission_batch_limit=batch_limit,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_directive_policy_reader_maps_only_complete_active_section() -> None:
    directives = AsyncMock(spec=DirectiveService)
    directives.get_active.return_value = _directive()

    read = await DirectiveSourcingAdmissionPolicyReader(directives).read(TENANT)

    assert read == SourcingAdmissionPolicyRead(
        directive_id="dir-active",
        directive_version=7,
        enabled=True,
        batch_limit=3,
    )
    directives.get_active.assert_awaited_once_with(TENANT)


@pytest.mark.asyncio
async def test_directive_policy_reader_returns_none_only_for_verified_missing_section() -> None:
    directives = AsyncMock(spec=DirectiveService)
    directives.get_active.side_effect = [None, _directive(mode=None, enabled=None, batch_limit=None)]
    reader = DirectiveSourcingAdmissionPolicyReader(directives)

    assert await reader.read(TENANT) is None
    assert await reader.read(TENANT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "directive",
    [
        _directive(mode="other"),
        _directive(enabled=1),
        _directive(batch_limit=True),
        _directive(batch_limit=0),
        _directive(batch_limit=51),
        _directive(mode=None),
    ],
)
async def test_directive_policy_reader_maps_partial_or_invalid_shape_to_fixed_transient(
    directive: DirectiveView,
) -> None:
    directives = AsyncMock(spec=DirectiveService)
    directives.get_active.return_value = directive

    with pytest.raises(
        TransientError, match="^寻源准入策略状态暂不可确认$"
    ) as caught:
        await DirectiveSourcingAdmissionPolicyReader(directives).read(TENANT)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_directive_policy_reader_sanitizes_storage_failure() -> None:
    directives = AsyncMock(spec=DirectiveService)
    directives.get_active.side_effect = RuntimeError(
        "postgres://user:secret@example.invalid/directives"
    )

    with pytest.raises(
        TransientError, match="^寻源准入策略状态暂不可确认$"
    ) as caught:
        await DirectiveSourcingAdmissionPolicyReader(directives).read(TENANT)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)


def _provenance() -> ProvenanceSummary:
    return ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-admission-driver",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-customer"),
        confirmed_at=NOW,
    )


def _snapshot(
    need_id: ValidatedNeedId,
    *,
    label: str = "Industrial Hinges",
    snapshot_hash: str = "a" * 64,
) -> SourcingNeedSnapshot:
    return SourcingNeedSnapshot(
        need_id=need_id,
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(value=label, provenance=_provenance()),
        application=NeedFact(value=f"{label} Application", provenance=_provenance()),
        material=NeedFact(value=f"{label} Material", provenance=_provenance()),
        size_spec=NeedFact(value=f"{label} Size", provenance=_provenance()),
        quantity=NeedFact(value=5000, provenance=_provenance()),
        snapshot_hash=snapshot_hash,
    )


def _admission(suffix: str) -> SourcingAdmission:
    need_id = ValidatedNeedId(f"need-{suffix}")
    return SourcingAdmission(
        tenant_id=TENANT,
        admission_id=SourcingAdmissionId(f"sad-{suffix}"),
        case_id=SourcingCaseId(f"src-{suffix}"),
        need_id=need_id,
        state=AdmissionState.WAITING,
        ready_at=NOW - timedelta(days=1),
        current_snapshot_id=f"sps-{suffix}",  # type: ignore[arg-type]
        claim_token=None,
        claim_expires_at=None,
        workflow_run_id=None,
        blocked_reason=None,
        admitted_at=None,
        admitted_by=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _case_view(
    admission: SourcingAdmission,
    *,
    need_id: ValidatedNeedId | None = None,
    state: str = "opened",
    workflow_version: int = 2,
    snapshot: SourcingNeedSnapshot | None = None,
) -> SourcingCaseReadView:
    return SourcingCaseReadView(
        case_id=admission.case_id,
        need_id=need_id or admission.need_id,
        state=state,
        workflow_version=workflow_version,
        version=1,
        opened_at=NOW,
        need_snapshot=snapshot or _snapshot(admission.need_id),
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
    def __init__(self, admissions: list[SourcingAdmission]) -> None:
        self.rows = admissions
        self.case_views = {row.case_id: _case_view(row) for row in admissions}
        self.case_errors: dict[SourcingCaseId, Exception] = {}
        self.claim_calls: list[dict[str, object]] = []
        self.release_expired_calls: list[datetime] = []
        self.complete_calls: list[dict[str, object]] = []
        self.release_calls: list[dict[str, object]] = []
        self.block_calls: list[dict[str, object]] = []
        self.complete_errors: deque[Exception | None] = deque()

    async def get_admission_case_snapshot(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        *,
        actor: SourcingActor,
    ) -> SourcingCaseReadView | None:
        assert (tenant_id, actor) == (TENANT, SYSTEM)
        if case_id in self.case_errors:
            raise self.case_errors[case_id]
        return self.case_views.get(case_id)

    async def release_expired_admission_claims(
        self, tenant_id: TenantId, *, now: datetime, actor: SourcingActor
    ) -> list[SourcingAdmission]:
        assert (tenant_id, actor) == (TENANT, SYSTEM)
        self.release_expired_calls.append(now)
        released: list[SourcingAdmission] = []
        for index, row in enumerate(self.rows):
            updated = row.release_expired_claim(now=now)
            if updated is not row:
                self.rows[index] = updated
                released.append(updated)
        return released

    async def claim_admissions(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        claim_token: str,
        claim_expires_at: datetime,
        actor: SourcingActor,
    ) -> list[SourcingAdmission]:
        assert (tenant_id, actor) == (TENANT, SYSTEM)
        self.claim_calls.append(
            {
                "limit": limit,
                "claim_token": claim_token,
                "claim_expires_at": claim_expires_at,
            }
        )
        claimed: list[SourcingAdmission] = []
        for index, row in enumerate(self.rows):
            if row.state is not AdmissionState.WAITING or len(claimed) >= limit:
                continue
            updated = row.claim(
                claim_token,
                claim_expires_at=claim_expires_at,
                claimed_at=claim_expires_at - LEASE,
            )
            self.rows[index] = updated
            claimed.append(updated)
        return claimed

    async def complete_admission(self, tenant_id: TenantId, admission_id, **kwargs):
        assert tenant_id == TENANT
        self.complete_calls.append({"admission_id": admission_id, **kwargs})
        error = self.complete_errors.popleft() if self.complete_errors else None
        if error is not None:
            raise error
        row = self._row(admission_id)
        self._replace(
            row.complete(
                kwargs["claim_token"],
                workflow_run_id=kwargs["workflow_run_id"],
                admitted_by=kwargs["admitted_by"],
                admitted_at=kwargs["admitted_at"],
            )
        )

    async def release_admission_claim(self, tenant_id: TenantId, admission_id, **kwargs):
        assert tenant_id == TENANT
        self.release_calls.append({"admission_id": admission_id, **kwargs})
        row = self._row(admission_id)
        self._replace(
            row._transition(
                AdmissionState.WAITING,
                changed_at=kwargs["released_at"],
                claim_token=None,
                claim_expires_at=None,
            )
        )

    async def block_admission(self, tenant_id: TenantId, admission_id, **kwargs):
        assert tenant_id == TENANT
        self.block_calls.append({"admission_id": admission_id, **kwargs})
        row = self._row(admission_id)
        self._replace(
            row.block(
                kwargs["reason"],
                blocked_at=kwargs["blocked_at"],
                claim_token=kwargs["claim_token"],
            )
        )

    def _row(self, admission_id: SourcingAdmissionId) -> SourcingAdmission:
        return next(row for row in self.rows if row.admission_id == admission_id)

    def _replace(self, updated: SourcingAdmission) -> None:
        index = next(
            index
            for index, row in enumerate(self.rows)
            if row.admission_id == updated.admission_id
        )
        self.rows[index] = updated


class _Engine:
    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.outcomes = deque(outcomes or [])
        self.calls: list[tuple[object, ...]] = []
        self.runs: dict[str, RunId] = {}

    async def start(self, *args: object) -> RunId:
        self.calls.append(args)
        if self.outcomes:
            outcome = self.outcomes.popleft()
            if isinstance(outcome, Exception):
                raise outcome
        key = str(args[4])
        return self.runs.setdefault(key, RunId(f"run-{len(self.runs) + 1}"))


def _driver(
    policy: _Policy,
    sourcing: _Sourcing,
    engine: _Engine,
    clock: _Clock,
) -> SourcingAdmissionDriver:
    return SourcingAdmissionDriver(
        policy=policy,  # type: ignore[arg-type]
        sourcing=sourcing,  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
        lease_duration=LEASE,
        now=clock.now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (_Policy(None), "policy_not_configured"),
        (
            _Policy(SourcingAdmissionPolicyRead("dir", 1, False, 7)),
            "automatic_admission_disabled",
        ),
        (
            _Policy(error=TransientError("storage-secret")),
            "policy_status_unknown",
        ),
    ],
)
async def test_missing_disabled_and_unknown_policy_are_distinct_and_start_nothing(
    policy: _Policy,
    reason: str,
) -> None:
    row = _admission("a")
    sourcing, engine, clock = _Sourcing([row]), _Engine(), _Clock()
    result = await _driver(policy, sourcing, engine, clock).scan_once()

    assert result.stop_reason == reason
    assert result.claimed_count == result.admitted_count == 0
    assert sourcing.claim_calls == sourcing.release_expired_calls == []
    assert engine.calls == []


@pytest.mark.asyncio
async def test_enabled_policy_claims_exact_policy_limit_and_preserves_repository_order() -> None:
    rows = [_admission("z"), _admission("a"), _admission("m")]
    sourcing, engine, clock = _Sourcing(rows), _Engine(), _Clock()
    driver = _driver(
        _Policy(SourcingAdmissionPolicyRead("dir", 9, True, 2)),
        sourcing,
        engine,
        clock,
    )

    result = await driver.scan_once()

    assert sourcing.claim_calls[0]["limit"] == 2
    assert len(str(sourcing.claim_calls[0]["claim_token"])) >= 16
    assert sourcing.claim_calls[0]["claim_expires_at"] == NOW + LEASE
    assert [call[2] for call in engine.calls] == ["src-z", "src-a"]
    assert engine.calls == [
        (
            TENANT,
            "sourcing_case",
            "src-z",
            _safe_context(_snapshot(rows[0].need_id), "src-z"),
            f"sourcing-case:v2:{TENANT}:{rows[0].need_id}",
        ),
        (
            TENANT,
            "sourcing_case",
            "src-a",
            _safe_context(_snapshot(rows[1].need_id), "src-a"),
            f"sourcing-case:v2:{TENANT}:{rows[1].need_id}",
        ),
    ]
    assert result.stop_reason == "batch_processed"
    assert (result.claimed_count, result.admitted_count) == (2, 2)
    assert sourcing.rows[2].state is AdmissionState.WAITING


@pytest.mark.asyncio
async def test_run_context_uses_frozen_case_snapshot() -> None:
    """把 context 改由其他来源重建会偷换 Case 已冻结的事实。"""

    row = _admission("frozen")
    frozen = _snapshot(row.need_id, label="Frozen A", snapshot_hash="a" * 64)
    sourcing, engine, clock = _Sourcing([row]), _Engine(), _Clock()
    sourcing.case_views[row.case_id] = _case_view(row, snapshot=frozen)

    result = await _driver(
        _Policy(SourcingAdmissionPolicyRead("dir", 1, True, 1)),
        sourcing,
        engine,
        clock,
    ).scan_once()

    assert result.admitted_count == 1
    assert engine.calls[0][3] == {
        "case_id": "src-frozen",
        "need_id": "need-frozen",
        "need_snapshot_hash": "a" * 64,
        "product_category": "frozen a",
        "keywords": [
            "frozen a application",
            "frozen a material",
            "frozen a size",
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "terminal",
        "non_opened",
        "non_v2",
        "case_need_mismatch",
        "snapshot_need_mismatch",
        "missing_snapshot",
        "missing_hash",
    ],
)
async def test_canonical_case_mismatch_blocks_without_start(fault: str) -> None:
    """Case 终态、版本、Need 或 frozen snapshot 任一失配都不能启动。"""

    row = _admission(fault)
    sourcing, engine, clock = _Sourcing([row]), _Engine(), _Clock()
    case = _case_view(row)
    if fault == "terminal":
        case = case.model_copy(update={"state": "failed"})
    elif fault == "non_opened":
        case = case.model_copy(update={"state": "discovering"})
    elif fault == "non_v2":
        case = case.model_copy(update={"workflow_version": 1})
    elif fault == "case_need_mismatch":
        case = case.model_copy(update={"need_id": ValidatedNeedId("need-other")})
    elif fault == "snapshot_need_mismatch":
        assert case.need_snapshot is not None
        case = case.model_copy(
            update={
                "need_snapshot": case.need_snapshot.model_copy(
                    update={"need_id": ValidatedNeedId("need-other")}
                )
            }
        )
    elif fault == "missing_snapshot":
        case = case.model_copy(update={"need_snapshot": None})
    else:
        assert case.need_snapshot is not None
        case = case.model_copy(
            update={
                "need_snapshot": case.need_snapshot.model_copy(
                    update={"snapshot_hash": None}
                )
            }
        )
    sourcing.case_views[row.case_id] = case

    result = await _driver(
        _Policy(SourcingAdmissionPolicyRead("dir", 1, True, 1)),
        sourcing,
        engine,
        clock,
    ).scan_once()

    assert result.blocked_count == 1
    assert engine.calls == []
    assert sourcing.rows[0].state is AdmissionState.BLOCKED
    assert sourcing.rows[0].blocked_reason is AdmissionBlockedReason.CASE_STATE_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_state", "release_count", "pending_count"),
    [
        (TransientError("canonical-private"), AdmissionState.WAITING, 1, 0),
        (ValidationError("canonical-private"), AdmissionState.BLOCKED, 0, 0),
        (RuntimeError("canonical-private"), AdmissionState.STARTING, 0, 1),
    ],
)
async def test_canonical_case_read_failure_is_sanitized_and_conservative(
    error: Exception,
    expected_state: AdmissionState,
    release_count: int,
    pending_count: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = _admission(expected_state.value)
    sourcing, engine, clock = _Sourcing([row]), _Engine(), _Clock()
    sourcing.case_errors[row.case_id] = error
    caplog.set_level(logging.INFO, logger="apps.scheduler_worker.sourcing_admission")

    result = await _driver(
        _Policy(SourcingAdmissionPolicyRead("dir", 1, True, 1)),
        sourcing,
        engine,
        clock,
    ).scan_once()

    assert sourcing.rows[0].state is expected_state
    assert len(sourcing.release_calls) == release_count
    assert result.pending_recovery_count == pending_count
    assert engine.calls == []
    assert "canonical-private" not in caplog.text


@pytest.mark.asyncio
async def test_claimed_items_are_isolated_by_transient_permanent_and_unknown_start_result() -> None:
    rows = [_admission("transient"), _admission("permanent"), _admission("unknown")]
    sourcing = _Sourcing(rows)
    engine = _Engine(
        [
            TransientError("temporary-secret"),
            ValidationError("permanent-secret"),
            RuntimeError("unknown-secret"),
        ]
    )
    clock = _Clock()
    result = await _driver(
        _Policy(SourcingAdmissionPolicyRead("dir", 1, True, 3)),
        sourcing,
        engine,
        clock,
    ).scan_once()

    assert [row.state for row in sourcing.rows] == [
        AdmissionState.WAITING,
        AdmissionState.BLOCKED,
        AdmissionState.STARTING,
    ]
    assert sourcing.rows[1].blocked_reason is AdmissionBlockedReason.CASE_STATE_MISMATCH
    assert [call["admission_id"] for call in sourcing.release_calls] == [
        rows[0].admission_id
    ]
    assert (
        result.returned_to_waiting_count,
        result.blocked_count,
        result.pending_recovery_count,
    ) == (1, 1, 1)


@pytest.mark.asyncio
async def test_start_before_bind_failure_recovers_one_canonical_run_after_lease() -> None:
    row = _admission("crash")
    sourcing, engine, clock = _Sourcing([row]), _Engine(), _Clock()
    sourcing.complete_errors.append(TransientError("bind-private"))
    driver = _driver(
        _Policy(SourcingAdmissionPolicyRead("dir", 1, True, 1)),
        sourcing,
        engine,
        clock,
    )

    first = await driver.scan_once()
    assert first.pending_recovery_count == 1
    assert sourcing.rows[0].state is AdmissionState.STARTING

    clock.value = NOW + LEASE
    second = await driver.scan_once()

    assert second.expired_released_count == 1
    assert second.admitted_count == 1
    assert len(engine.runs) == 1
    assert engine.calls[0][4] == engine.calls[1][4]
    assert sourcing.rows[0].workflow_run_id == next(iter(engine.runs.values()))


@pytest.mark.asyncio
async def test_driver_logs_only_fixed_reason_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = _admission("log")
    sourcing, engine, clock = _Sourcing([row]), _Engine([RuntimeError("raw-token")]), _Clock()
    caplog.set_level(logging.INFO, logger="apps.scheduler_worker.sourcing_admission")

    await _driver(
        _Policy(SourcingAdmissionPolicyRead("dir", 1, True, 1)),
        sourcing,
        engine,
        clock,
    ).scan_once()

    assert "raw-token" not in caplog.text
    assert sourcing.release_calls == []
    record = caplog.records[-1]
    assert {
        key
        for key in (
            "tenant_id",
            "stop_reason",
            "claimed_count",
            "admitted_count",
            "returned_to_waiting_count",
            "blocked_count",
            "pending_recovery_count",
        )
        if key in record.__dict__
    } == {
        "tenant_id",
        "stop_reason",
        "claimed_count",
        "admitted_count",
        "returned_to_waiting_count",
        "blocked_count",
        "pending_recovery_count",
    }
