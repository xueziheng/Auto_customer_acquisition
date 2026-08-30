"""公开候选草稿确定性核验与封存合同。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.sourcing.schemas import (
    VerifyPublicCandidateDraftsCommand,
    VerifyPublicCandidateDraftsResult,
)
from shared.events.catalog import SourcingCandidatesVerified
from shared.schemas.identifiers import (
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    SupplierCandidateId,
    TenantId,
)


def test_verification_command_binds_exact_ordered_draft_generation() -> None:
    command = VerifyPublicCandidateDraftsCommand(
        run_id=RunId("run-public-verification"),
        plan_id=SourcingPlanId("spl-public-verification"),
        plan_hash="a" * 64,
        draft_ids=("scd-first", "scd-second"),
    )

    assert command.draft_ids == ("scd-first", "scd-second")
    assert command.model_copy().model_config["frozen"] is True
    with pytest.raises(PydanticValidationError):
        VerifyPublicCandidateDraftsCommand(
            run_id=command.run_id,
            plan_id=command.plan_id,
            plan_hash=command.plan_hash,
            draft_ids=("scd-first", "scd-first"),
        )


def test_verification_result_separates_drafts_candidates_and_sealed_generation() -> None:
    result = VerifyPublicCandidateDraftsResult(
        calibration_draft_ids=("scd-incomplete",),
        converted_candidate_ids=("spc-qualified", "spc-rejected"),
        rejected_candidate_ids=("spc-rejected",),
        qualified_candidate_ids=(),
        verified_event=None,
    )

    assert result.calibration_draft_ids == ("scd-incomplete",)
    assert result.converted_candidate_ids == ("spc-qualified", "spc-rejected")
    assert result.rejected_candidate_ids == ("spc-rejected",)
    assert result.qualified_candidate_ids == ()


def test_verification_result_allows_preexisting_qualified_candidates() -> None:
    event = SourcingCandidatesVerified(
        tenant_id=TenantId("tenant-1"),
        occurred_at=datetime(2026, 8, 30, tzinfo=UTC),
        run_id=RunId("run-public-verification"),
        case_id=SourcingCaseId("src-case-1"),
        candidate_ids=(
            SupplierCandidateId("spc-existing"),
            SupplierCandidateId("spc-new"),
        ),
        case_version=3,
        candidate_set_hash="a" * 64,
    )

    result = VerifyPublicCandidateDraftsResult(
        calibration_draft_ids=(),
        converted_candidate_ids=(SupplierCandidateId("spc-new"),),
        rejected_candidate_ids=(),
        qualified_candidate_ids=event.candidate_ids,
        verified_event=event,
    )

    assert result.qualified_candidate_ids == event.candidate_ids
