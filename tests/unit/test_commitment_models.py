from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

from shared.schemas.identifiers import (
    CommitmentId,
    EmployeeId,
    MessageId,
    TenantId,
)

_models = importlib.import_module("domains.commitments.models")
Commitment = _models.Commitment
CommitmentStatus = _models.CommitmentStatus
CommitmentType = _models.CommitmentType

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _commitment(
    *,
    confirmed: bool = True,
    uncertain: bool = False,
    status: CommitmentStatus = CommitmentStatus.PENDING,
) -> Commitment:
    return Commitment(
        commitment_id=CommitmentId("commitment-one"),
        tenant_id=TenantId("tenant-one"),
        commitment_type=CommitmentType.EMPLOYEE,
        owner=EmployeeId("employee-one"),
        action="Send the updated quotation",
        due_at=NOW,
        source_message_id=MessageId("message-one"),
        verbatim="I will send the updated quotation tomorrow.",
        created_at=NOW - timedelta(days=1),
        status=status,
        due_at_uncertain=uncertain,
        confirmed_by=EmployeeId("manager-one") if confirmed else None,
        confirmed_at=NOW if confirmed else None,
    )


def test_commitment_confirmation_depends_on_human_confirmation() -> None:
    assert _commitment(confirmed=False).is_confirmed is False
    assert _commitment(confirmed=True).is_confirmed is True

    incomplete = _commitment(confirmed=False)
    incomplete.confirmed_by = EmployeeId("manager-one")
    assert incomplete.is_confirmed is False


def test_only_confirmed_certain_open_commitments_become_overdue() -> None:
    assert _commitment().is_overdue_at(NOW - timedelta(microseconds=1)) is False
    assert _commitment().is_overdue_at(NOW) is True
    assert _commitment(confirmed=False).is_overdue_at(NOW) is False
    assert _commitment(uncertain=True).is_overdue_at(NOW) is False
    assert _commitment(status=CommitmentStatus.FULFILLED).is_overdue_at(NOW) is False
    assert _commitment(status=CommitmentStatus.CANCELLED).is_overdue_at(NOW) is False


def test_already_overdue_commitment_remains_overdue_for_escalation() -> None:
    assert _commitment(status=CommitmentStatus.OVERDUE).is_overdue_at(NOW) is True
