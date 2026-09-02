"""Phase 2 寻源触发事件合同。"""

from datetime import UTC, datetime

from shared.events.catalog import (
    NeedBecameSourcingReady,
    NeedClusterMembershipChanged,
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
    SourcingCaseHandedToCosting,
)
from shared.schemas.identifiers import (
    NeedClusterId,
    OpportunityId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPrioritySnapshotId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def test_need_cluster_membership_event_uses_admission_and_priority_identifiers() -> None:
    """需求簇成员变更保留后续 admission 与优先级快照所需的强类型 ID。"""
    admission_id = SourcingAdmissionId("sad_0" + "A" * 25)
    priority_snapshot_id = SourcingPrioritySnapshotId("sps_0" + "B" * 25)
    event = NeedClusterMembershipChanged(
        tenant_id=TenantId("tn_0" + "C" * 25),
        occurred_at=NOW,
        cluster_id=NeedClusterId("ncl_0" + "D" * 25),
        changed_need_id=ValidatedNeedId("vnd_0" + "E" * 25),
        member_count=2,
    )

    assert admission_id == "sad_0" + "A" * 25
    assert priority_snapshot_id == "sps_0" + "B" * 25
    assert event.cluster_id == NeedClusterId("ncl_0" + "D" * 25)
    assert event.changed_need_id == ValidatedNeedId("vnd_0" + "E" * 25)
    assert event.member_count == 2


def test_phase2_sourcing_events_are_past_tense_tenant_bound_facts() -> None:
    """三个事件只携带已发生事实所需的 tenant-bound 标识。"""
    ready = NeedBecameSourcingReady(
        tenant_id=TenantId("tenant-a"),
        occurred_at=NOW,
        need_id=ValidatedNeedId("need-a"),
        completeness=3,
    )
    verified = SourcingCandidatesVerified(
        tenant_id=TenantId("tenant-a"),
        occurred_at=NOW,
        case_id=SourcingCaseId("src-a"),
        candidate_ids=(SupplierCandidateId("sc-a"),),
        case_version=8,
        candidate_set_hash="a" * 64,
    )
    candidates = SourcingCandidatesReady(
        tenant_id=TenantId("tenant-a"),
        occurred_at=NOW,
        case_id=SourcingCaseId("src-a"),
        option_ids=(SourcingSupplyOptionId("sop-a"),),
        candidate_ids=(SupplierCandidateId("sc-a"),),
    )
    handed = SourcingCaseHandedToCosting(
        tenant_id=TenantId("tenant-a"),
        occurred_at=NOW,
        case_id=SourcingCaseId("src-a"),
        need_id=ValidatedNeedId("need-a"),
        opportunity_id=OpportunityId("opp-a"),
        review_id=SourcingReviewId("srv-a"),
    )

    assert ready.completeness == 3
    assert verified.candidate_ids == (SupplierCandidateId("sc-a"),)
    assert set(vars(verified)) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "case_id",
        "candidate_ids",
        "case_version",
        "candidate_set_hash",
    }
    assert candidates.option_ids == (SourcingSupplyOptionId("sop-a"),)
    assert candidates.candidate_ids == (SupplierCandidateId("sc-a"),)
    assert handed.opportunity_id == OpportunityId("opp-a")
    assert handed.review_id == SourcingReviewId("srv-a")
