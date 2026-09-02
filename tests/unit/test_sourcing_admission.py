"""寻源准入的纯领域不变量与固定排序。"""

from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone

import pytest

from domains.sourcing.admission import (
    ADMISSION_RANKING_VERSION,
    AdmissionBlockedReason,
    AdmissionState,
    SourcingAdmission,
    SourcingPrioritySnapshot,
    priority_explanation,
    priority_sort_key,
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
    facts_hash: str = "a" * 64,
) -> SourcingPrioritySnapshot:
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
        facts_observed_at=NOW,
        facts_hash=facts_hash,
        created_at=NOW,
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


@pytest.mark.parametrize("facts_hash", ["A" * 64, "a" * 63, "g" * 64])
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


def test_admitted_admission_is_immutable_and_cannot_be_reclaimed() -> None:
    admitted = _admission().claim("claim-1", claim_expires_at=NOW + timedelta(minutes=5), claimed_at=NOW).complete(
        "claim-1", workflow_run_id=RunId("run-1"), admitted_by="system:sourcing", admitted_at=NOW
    )

    with pytest.raises(InvalidStateTransition, match="admitted"):
        admitted.claim("claim-2", claim_expires_at=NOW + timedelta(minutes=10), claimed_at=NOW)
    with pytest.raises(InvalidStateTransition, match="admitted"):
        admitted.with_current_snapshot(_snapshot())


def test_priority_explanation_is_deterministic_chinese_text() -> None:
    assert priority_explanation(_snapshot()) == "该需求簇当前有 8 条已验证需求；同规模需求按等待时间排序。"
    assert priority_explanation(_snapshot(count=1, cluster_id=None)) == "该需求尚未归入多成员需求簇；按等待时间排序。"
