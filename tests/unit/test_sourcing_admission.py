"""寻源准入的纯领域不变量与固定排序。"""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.sourcing.admission import (
    ADMISSION_RANKING_VERSION,
    AdmissionBlockedReason,
    AdmissionState,
    SourcingAdmission,
    SourcingPrioritySnapshot,
    canonical_priority_facts_hash,
    priority_explanation,
    priority_facts_payload,
    priority_sort_key,
)
from domains.sourcing.schemas import (
    SourcingAdmissionManualStartCommand,
    SourcingAdmissionReadView,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import (
    NeedClusterId,
    RunId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPrioritySnapshotId,
    TenantId,
    ValidatedNeedId,
)

TENANT = TenantId("tenant-admission")
NOW = datetime(2026, 9, 2, 8, tzinfo=UTC)
READY_AT = datetime(2026, 9, 1, 8, tzinfo=UTC)
NEED_ID = ValidatedNeedId("need-8")
CLUSTER_ID = NeedClusterId("cluster-a")
CURRENT_SNAPSHOT_ID = SourcingPrioritySnapshotId("snapshot-need-8")


def _snapshot(
    *,
    count: int = 8,
    cluster_id: NeedClusterId | None = CLUSTER_ID,
    need_id: ValidatedNeedId = NEED_ID,
    ready_at: datetime = READY_AT,
    facts_hash: str | None = None,
    facts_observed_at: datetime = NOW,
    created_at: datetime = NOW,
) -> SourcingPrioritySnapshot:
    canonical_hash = canonical_priority_facts_hash(
        need_id=need_id,
        cluster_id=cluster_id,
        cluster_member_count=count,
        ready_at=ready_at,
        facts_observed_at=facts_observed_at,
        ranking_version=ADMISSION_RANKING_VERSION,
    )
    return SourcingPrioritySnapshot(
        tenant_id=TENANT,
        snapshot_id=SourcingPrioritySnapshotId(f"snapshot-{need_id}"),
        admission_id=SourcingAdmissionId(f"admission-{need_id}"),
        case_id=SourcingCaseId(f"case-{need_id}"),
        need_id=need_id,
        cluster_id=cluster_id,
        cluster_member_count=count,
        ready_at=ready_at,
        ranking_version=ADMISSION_RANKING_VERSION,
        facts_observed_at=facts_observed_at,
        facts_hash=canonical_hash if facts_hash is None else facts_hash,
        created_at=created_at,
    )


def _admission(
    *, state: AdmissionState = AdmissionState.WAITING,
    current_snapshot_id: SourcingPrioritySnapshotId | None = CURRENT_SNAPSHOT_ID,
    claim_token: str | None = None,
    claim_expires_at: datetime | None = None,
    workflow_run_id: RunId | None = None,
    blocked_reason: AdmissionBlockedReason | None = None,
    admitted_at: datetime | None = None,
    admitted_by: str | None = None,
) -> SourcingAdmission:
    return SourcingAdmission(
        tenant_id=TENANT,
        admission_id=SourcingAdmissionId("admission-need-8"),
        case_id=SourcingCaseId("case-need-8"),
        need_id=NEED_ID,
        state=state,
        ready_at=READY_AT,
        current_snapshot_id=current_snapshot_id,
        claim_token=claim_token,
        claim_expires_at=claim_expires_at,
        workflow_run_id=workflow_run_id,
        blocked_reason=blocked_reason,
        admitted_at=admitted_at,
        admitted_by=admitted_by,
        created_at=NOW,
        updated_at=NOW,
    )


def test_priority_key_is_exact_cluster_count_then_utc_wait_then_need_id() -> None:
    snapshot = _snapshot()

    assert priority_sort_key(snapshot) == (-8, READY_AT, str(NEED_ID))


def test_priority_order_is_stable_for_clustered_and_unclustered_needs() -> None:
    snapshots = (
        _snapshot(count=1, cluster_id=None, need_id=ValidatedNeedId("need-z")),
        _snapshot(count=3, need_id=ValidatedNeedId("need-b")),
        _snapshot(count=8, need_id=ValidatedNeedId("need-c")),
        _snapshot(count=8, need_id=ValidatedNeedId("need-a")),
    )

    ordered = sorted(snapshots, key=priority_sort_key)

    assert [item.need_id for item in ordered] == [
        ValidatedNeedId("need-a"),
        ValidatedNeedId("need-c"),
        ValidatedNeedId("need-b"),
        ValidatedNeedId("need-z"),
    ]


def test_priority_order_uses_ready_at_before_need_id_with_equal_counts() -> None:
    early = _snapshot(need_id=ValidatedNeedId("need-z"), ready_at=READY_AT)
    late = _snapshot(
        need_id=ValidatedNeedId("need-a"), ready_at=READY_AT + timedelta(seconds=1)
    )

    assert [item.need_id for item in sorted((late, early), key=priority_sort_key)] == [
        ValidatedNeedId("need-z"),
        ValidatedNeedId("need-a"),
    ]


def test_unclustered_snapshot_has_count_one_and_all_times_are_utc() -> None:
    with pytest.raises(ValidationError, match="未归簇"):
        _snapshot(count=2, cluster_id=None)
    with pytest.raises(ValidationError, match="UTC"):
        _snapshot(ready_at=datetime(2026, 9, 1, 8))  # noqa: DTZ001
    with pytest.raises(ValidationError, match="UTC"):
        _snapshot(
            facts_hash="a" * 64,
            ready_at=datetime(2026, 9, 1, 16, tzinfo=timezone(timedelta(hours=8))),
        )


@pytest.mark.parametrize("facts_hash", ["a" * 64, "A" * 64, "a" * 63, "g" * 64])
def test_snapshot_rejects_noncanonical_sha256_hashes(facts_hash: str) -> None:
    with pytest.raises(ValidationError, match="facts_hash"):
        _snapshot(facts_hash=facts_hash)


def test_priority_snapshot_never_contains_quantity_or_other_aggregated_trade_fields() -> None:
    names = {field.name for field in fields(SourcingPrioritySnapshot)}

    assert "quantity" not in names
    assert "unit" not in names
    assert "country" not in names
    assert "specification" not in names
    assert "provenance" not in names


@pytest.mark.parametrize(
    ("state", "claim_token", "claim_expires_at", "workflow_run_id", "blocked_reason", "admitted_at", "admitted_by"),
    [
        (AdmissionState.WAITING, "claim", NOW, None, None, None, None),
        (AdmissionState.STARTING, None, NOW, None, None, None, None),
        (AdmissionState.ADMITTED, None, None, None, None, NOW, "system:sourcing"),
        (AdmissionState.BLOCKED, None, None, None, None, None, None),
    ],
)
def test_admission_rejects_invalid_state_field_combinations(
    state: AdmissionState,
    claim_token: str | None,
    claim_expires_at: datetime | None,
    workflow_run_id: RunId | None,
    blocked_reason: AdmissionBlockedReason | None,
    admitted_at: datetime | None,
    admitted_by: str | None,
) -> None:
    with pytest.raises(ValidationError):
        _admission(
            state=state,
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
            workflow_run_id=workflow_run_id,
            blocked_reason=blocked_reason,
            admitted_at=admitted_at,
            admitted_by=admitted_by,
        )


def test_claim_then_expired_lease_returns_admission_to_waiting() -> None:
    claimed = _admission().claim("claim-1", claim_expires_at=NOW + timedelta(minutes=5), claimed_at=NOW)

    assert claimed.state is AdmissionState.STARTING
    assert claimed.release_expired_claim(now=NOW + timedelta(minutes=5)).state is AdmissionState.WAITING


def test_starting_block_requires_matching_claim_token_and_waiting_does_not() -> None:
    claimed = _admission().claim(
        "claim-current", claim_expires_at=NOW + timedelta(minutes=5), claimed_at=NOW
    )

    with pytest.raises(InvalidStateTransition):
        claimed.block(AdmissionBlockedReason.PRIORITY_FACTS_INVALID, blocked_at=NOW)
    with pytest.raises(InvalidStateTransition):
        claimed.block(
            AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
            blocked_at=NOW,
            claim_token="claim-stale",
        )
    assert claimed.block(
        AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
        blocked_at=NOW,
        claim_token="claim-current",
    ).state is AdmissionState.BLOCKED
    assert _admission().block(
        AdmissionBlockedReason.CASE_STATE_MISMATCH, blocked_at=NOW
    ).state is AdmissionState.BLOCKED


def test_canonical_priority_hash_binds_exact_facts_and_nullable_cluster() -> None:
    payload = priority_facts_payload(
        need_id=NEED_ID,
        cluster_id=None,
        cluster_member_count=1,
        ready_at=READY_AT,
        facts_observed_at=NOW,
        ranking_version=ADMISSION_RANKING_VERSION,
    )
    again = canonical_priority_facts_hash(
        need_id=NEED_ID,
        cluster_id=None,
        cluster_member_count=1,
        ready_at=READY_AT,
        facts_observed_at=NOW,
        ranking_version=ADMISSION_RANKING_VERSION,
    )

    assert payload == {
        "need_id": "need-8",
        "cluster_id": None,
        "cluster_member_count": 1,
        "ready_at": "2026-09-01T08:00:00+00:00",
        "facts_observed_at": "2026-09-02T08:00:00+00:00",
        "ranking_version": "need-cluster-admission-v1",
    }
    assert again == canonical_priority_facts_hash(
        need_id=NEED_ID,
        cluster_id=None,
        cluster_member_count=1,
        ready_at=READY_AT,
        facts_observed_at=NOW,
        ranking_version=ADMISSION_RANKING_VERSION,
    )


def test_admission_transitions_and_snapshot_refresh_never_move_time_backwards() -> None:
    with pytest.raises(ValidationError, match="updated_at"):
        _admission().claim(
            "claim-1", claim_expires_at=NOW + timedelta(minutes=5), claimed_at=NOW - timedelta(seconds=1)
        )
    claimed = _admission().claim(
        "claim-1", claim_expires_at=NOW + timedelta(minutes=5), claimed_at=NOW
    )
    with pytest.raises(ValidationError, match="updated_at"):
        claimed.complete(
            "claim-1", workflow_run_id=RunId("run-1"), admitted_by="system:sourcing", admitted_at=NOW - timedelta(seconds=1)
        )
    with pytest.raises(ValidationError, match="updated_at"):
        _admission().with_current_snapshot(_snapshot(created_at=NOW - timedelta(seconds=1)))
    with pytest.raises(ValidationError, match="ready_at"):
        _admission().with_current_snapshot(_snapshot(ready_at=READY_AT + timedelta(seconds=1)))


def test_blocked_retry_and_valid_snapshot_refresh_only_auto_recovers_invalid_facts() -> None:
    invalid = _admission().block(AdmissionBlockedReason.PRIORITY_FACTS_INVALID, blocked_at=NOW)
    refreshed = invalid.with_current_snapshot(_snapshot())

    assert refreshed.state is AdmissionState.WAITING
    mismatch = _admission().block(AdmissionBlockedReason.CASE_STATE_MISMATCH, blocked_at=NOW)
    assert mismatch.with_current_snapshot(_snapshot()).state is AdmissionState.BLOCKED
    assert mismatch.retry(retried_at=NOW).state is AdmissionState.WAITING


def test_snapshot_refresh_rejects_cross_tenant_case_or_need_bindings() -> None:
    admission = _admission()
    valid = _snapshot()

    for snapshot in (
            replace(valid, tenant_id=TenantId("tenant-other")),
            replace(valid, case_id=SourcingCaseId("case-other")),
            _snapshot(need_id=ValidatedNeedId("need-other")),
    ):
        with pytest.raises(ValidationError, match="不一致"):
            admission.with_current_snapshot(snapshot)


def test_safe_view_requires_all_snapshot_facts_and_fixed_explanation() -> None:
    snapshot = _snapshot()
    values = {
        "admission_id": SourcingAdmissionId("admission-need-8"),
        "case_id": SourcingCaseId("case-need-8"),
        "need_id": NEED_ID,
        "state": "waiting",
        "blocked_reason": None,
        "snapshot_id": snapshot.snapshot_id,
        "cluster_id": snapshot.cluster_id,
        "cluster_member_count": snapshot.cluster_member_count,
        "ready_at": snapshot.ready_at,
        "facts_observed_at": snapshot.facts_observed_at,
        "ranking_version": snapshot.ranking_version,
        "explanation": priority_explanation(snapshot),
        "waiting_duration_seconds": 60,
        "admitted_at": None,
        "admitted_by": None,
        "can_current_user_manual_start": True,
    }

    assert SourcingAdmissionReadView(**values).snapshot_id == snapshot.snapshot_id
    with pytest.raises(PydanticValidationError):
        SourcingAdmissionReadView(**(values | {"explanation": "任意文字"}))
    with pytest.raises(PydanticValidationError):
        SourcingAdmissionReadView(**(values | {"snapshot_id": None}))
    no_snapshot = values | {
        "snapshot_id": None,
        "cluster_id": None,
        "cluster_member_count": None,
        "ready_at": None,
        "facts_observed_at": None,
        "ranking_version": None,
        "explanation": None,
        "waiting_duration_seconds": None,
    }
    with pytest.raises(PydanticValidationError):
        SourcingAdmissionReadView(**no_snapshot)
    assert SourcingAdmissionReadView(
        **(
            no_snapshot
            | {
                "state": "blocked",
                "blocked_reason": "priority_facts_invalid",
            }
        )
    ).snapshot_id is None
    names = set(SourcingAdmissionReadView.model_fields)
    assert {"claim_token", "claim_expires_at", "workflow_context"}.isdisjoint(names)


def test_admitted_admission_is_immutable_and_cannot_be_reclaimed() -> None:
    admitted = _admission().claim("claim-1", claim_expires_at=NOW + timedelta(minutes=5), claimed_at=NOW).complete(
        "claim-1", workflow_run_id=RunId("run-1"), admitted_by="system:sourcing", admitted_at=NOW
    )

    with pytest.raises(InvalidStateTransition, match="admitted"):
        admitted.claim("claim-2", claim_expires_at=NOW + timedelta(minutes=10), claimed_at=NOW)
    with pytest.raises(InvalidStateTransition, match="admitted"):
        admitted.with_current_snapshot(_snapshot())


def test_stale_claim_token_cannot_complete_or_block_a_starting_admission() -> None:
    claimed = _admission().claim(
        "claim-current", claim_expires_at=NOW + timedelta(minutes=5), claimed_at=NOW
    )

    with pytest.raises(InvalidStateTransition):
        claimed.complete(
            "claim-stale",
            workflow_run_id=RunId("run-1"),
            admitted_by="system:sourcing",
            admitted_at=NOW,
        )
    with pytest.raises(InvalidStateTransition):
        claimed.block(
            AdmissionBlockedReason.CASE_STATE_MISMATCH,
            claim_token="claim-stale",
            blocked_at=NOW,
        )


@pytest.mark.parametrize("invalid", ["bad\nvalue", "bad\x00value", " " * 201])
def test_admission_text_fields_and_public_request_id_reject_unsafe_values(
    invalid: str,
) -> None:
    with pytest.raises(ValidationError):
        _admission(
            state=AdmissionState.ADMITTED,
            workflow_run_id=RunId("run-1"),
            admitted_at=NOW,
            admitted_by=invalid,
        )
    with pytest.raises(PydanticValidationError):
        SourcingAdmissionManualStartCommand(request_id=invalid)


def test_priority_explanation_is_deterministic_chinese_text() -> None:
    assert priority_explanation(_snapshot()) == "该需求簇当前有 8 条已验证需求；同规模需求按等待时间排序。"
    assert priority_explanation(_snapshot(count=1, cluster_id=None)) == "该需求尚未归入多成员需求簇；按等待时间排序。"
