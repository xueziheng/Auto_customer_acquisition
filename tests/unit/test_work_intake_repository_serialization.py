"""员工工作 JSONB 反序列化必须恢复严格的版本模型。"""

from datetime import UTC, datetime

from infra.db.repositories.work_intake import _payload
from workflows.employee_work_intake.schemas import (
    ExtractedCommitment,
    ExtractedFact,
    ExtractionPayload,
)


def test_jsonb_payload_roundtrip_restores_tuples_and_datetimes() -> None:
    original = ExtractionPayload(
        facts=(
            ExtractedFact(
                fact_type="customer_statement",
                value="500 units",
                evidence_quote="We need 500 units",
            ),
        ),
        commitments=(
            ExtractedCommitment(
                commitment_type="employee",
                action="Send specification",
                due_at=datetime(2026, 8, 23, 9, tzinfo=UTC),
                due_at_uncertain=False,
                verbatim="I will send it tomorrow",
            ),
        ),
    )

    restored = _payload(original.model_dump(mode="json"))

    assert restored == original
    assert isinstance(restored.facts, tuple)
    assert isinstance(restored.commitments, tuple)
