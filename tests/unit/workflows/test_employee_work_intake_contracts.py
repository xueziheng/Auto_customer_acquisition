"""员工工作上传必须保留不可变的提取与人工确认版本链。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    TenantId,
    WorkExtractionId,
    WorkUploadId,
)
from workflows.employee_work_intake.schemas import (
    ExtractedCommitment,
    ExtractedFact,
    ExtractedNeedField,
    ExtractionPayload,
    ProgressNote,
    WorkExtractionView,
    WorkSourceKind,
    WorkUploadStatus,
    WorkUploadView,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)


def _payload() -> ExtractionPayload:
    return ExtractionPayload(
        facts=(
            ExtractedFact(
                fact_type="customer_statement",
                value="Customer needs 500 units",
                evidence_quote="We need 500 units",
            ),
        ),
        need_field_updates=(
            ExtractedNeedField(
                field_name="quantity",
                value="500",
                evidence_quote="We need 500 units",
            ),
        ),
        commitments=(
            ExtractedCommitment(
                commitment_type="employee",
                action="Send specification sheet",
                due_at=datetime(2026, 8, 23, 10, tzinfo=UTC),
                due_at_uncertain=False,
                verbatim="I will send the specification sheet tomorrow",
            ),
        ),
        progress_note=ProgressNote(
            summary="Customer quantity was clarified",
            evidence_quotes=("We need 500 units",),
        ),
    )


def test_upload_view_keeps_artifact_and_owner_but_not_raw_content() -> None:
    view = WorkUploadView(
        upload_id=WorkUploadId("upl_01K39P9M5D6K4A91YEQ80EJZ0X"),
        tenant_id=TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X"),
        artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        employee_id=EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X"),
        source_kind=WorkSourceKind.PDF_TEXT,
        status=WorkUploadStatus.UPLOADED,
        occurred_at=NOW,
        customer_timezone="Asia/Shanghai",
        created_at=NOW,
    )

    assert view.artifact_id.startswith("art_")
    assert "source_content" not in type(view).model_fields
    assert "object_key" not in type(view).model_fields


def test_extraction_payload_is_strict_and_evidence_bound() -> None:
    payload = _payload()

    assert payload.facts[0].evidence_quote == "We need 500 units"
    assert payload.commitments[0].due_at.tzinfo is UTC

    with pytest.raises(PydanticValidationError):
        ExtractedFact.model_validate(
            {
                "fact_type": "customer_statement",
                "value": "Customer needs 500 units",
                "evidence_quote": "We need 500 units",
                "confidence": 0.91,
            }
        )


def test_extraction_view_is_pending_until_a_human_confirmation_exists() -> None:
    extraction = WorkExtractionView(
        extraction_id=WorkExtractionId("wex_01K39P9M5D6K4A91YEQ80EJZ0X"),
        upload_id=WorkUploadId("upl_01K39P9M5D6K4A91YEQ80EJZ0X"),
        payload=_payload(),
        extracted_by="team-operations-v1",
        created_at=NOW,
        confirmation=None,
    )

    assert extraction.confirmation is None
    assert extraction.is_confirmed is False


def test_relative_commitment_time_and_empty_evidence_are_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        ExtractedCommitment(
            commitment_type="employee",
            action="Send specification sheet",
            due_at=NOW.replace(tzinfo=None),
            due_at_uncertain=False,
            verbatim="I will send it tomorrow",
        )

    with pytest.raises(PydanticValidationError):
        ExtractedFact(
            fact_type="activity",
            value="Call completed",
            evidence_quote=" ",
        )
