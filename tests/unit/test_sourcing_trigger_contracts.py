"""Phase 2 寻源触发事件合同。"""

from datetime import UTC, datetime

from shared.events.catalog import (
    NeedBecameSourcingReady,
    SourcingCandidatesReady,
    SourcingCaseHandedToCosting,
)
from shared.schemas.identifiers import (
    OpportunityId,
    SourcingCaseId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def test_phase2_sourcing_events_are_past_tense_tenant_bound_facts() -> None:
    """三个事件只携带已发生事实所需的 tenant-bound 标识。"""
    ready = NeedBecameSourcingReady(
        tenant_id=TenantId("tenant-a"),
        occurred_at=NOW,
        need_id=ValidatedNeedId("need-a"),
        completeness=3,
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
    assert candidates.option_ids == (SourcingSupplyOptionId("sop-a"),)
    assert candidates.candidate_ids == (SupplierCandidateId("sc-a"),)
    assert handed.opportunity_id == OpportunityId("opp-a")
    assert handed.review_id == SourcingReviewId("srv-a")
