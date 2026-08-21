from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.base import ChangeSet
from agent_runtime.guardrails.rails import (
    EvidenceRequiredRail,
    GuardrailChecker,
    NoProbabilityOutputRail,
    RailViolation,
    TenantConsistencyRail,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, RunId, TenantId


def _change_set() -> ChangeSet:
    return ChangeSet(
        change_set_id=ChangeSetId("change-set-one"),
        tenant_id=TenantId("tenant-one"),
        run_id=RunId("run-one"),
    )


class _Rail:
    def __init__(
        self,
        name: str,
        violations: list[RailViolation] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.violations = violations or []
        self.error = error
        self.calls: list[ChangeSet] = []

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        self.calls.append(change_set)
        if self.error is not None:
            raise self.error
        return self.violations


def test_checker_runs_every_rail_and_collects_all_violations() -> None:
    first_violation = RailViolation("first", "changes[0]", "bad one", "fix one")
    second_violation = RailViolation("second", "changes[1]", "bad two", "fix two")
    first = _Rail("first", [first_violation])
    second = _Rail("second", [second_violation])
    checker = GuardrailChecker()
    checker.register(first)
    checker.register(second)
    change_set = _change_set()

    result = checker.check_all(change_set)

    assert result.passed is False
    assert result.violations == [first_violation, second_violation]
    assert first.calls == [change_set]
    assert second.calls == [change_set]


def test_checker_passes_only_when_every_registered_rail_passes() -> None:
    checker = GuardrailChecker()
    checker.register(_Rail("clean"))

    result = checker.check_all(_change_set())

    assert result.passed is True
    assert result.violations == []


def test_checker_rejects_duplicate_names_and_fails_closed_on_rail_error() -> None:
    checker = GuardrailChecker()
    checker.register(_Rail("tenant_consistency"))
    with pytest.raises(ValidationError, match="护栏名称重复"):
        checker.register(_Rail("tenant_consistency"))

    failing = GuardrailChecker()
    failing.register(_Rail("price_basis", error=RuntimeError("private detail")))

    result = failing.check_all(_change_set())

    assert result.passed is False
    assert len(result.violations) == 1
    assert result.violations[0].rail == "price_basis"
    assert "private detail" not in result.violations[0].detail


@pytest.mark.parametrize("invalid", [None, object(), Any])
def test_checker_rejects_invalid_rail(invalid: object) -> None:
    with pytest.raises(ValidationError, match="护栏实现无效"):
        GuardrailChecker().register(invalid)  # type: ignore[arg-type]


def test_tenant_consistency_rail_reports_every_cross_tenant_location() -> None:
    change_set = _change_set()
    change_set.changes = [
        {
            "domain": "demand",
            "operation": "update",
            "tenant_id": "tenant-two",
            "payload": {
                "tenant_id": "tenant-one",
                "evidence": [{"tenant_id": "tenant-three"}],
            },
        },
        {"domain": "outreach", "payload": {"tenant_id": 7}},
    ]

    violations = TenantConsistencyRail().check(change_set)

    assert [violation.location for violation in violations] == [
        "changes[0].tenant_id",
        "changes[0].payload.evidence[0].tenant_id",
        "changes[1].payload.tenant_id",
    ]
    assert all(violation.rail == "tenant_consistency" for violation in violations)


def test_tenant_consistency_rail_allows_matching_or_absent_nested_tenant() -> None:
    change_set = _change_set()
    change_set.changes = [
        {
            "domain": "demand",
            "payload": {
                "tenant_id": "tenant-one",
                "items": [{"name": "hinge"}],
            },
        }
    ]

    assert TenantConsistencyRail().check(change_set) == []


def test_probability_rail_finds_numeric_fields_and_hidden_text_values() -> None:
    change_set = _change_set()
    change_set.changes = [
        {
            "payload": {
                "confidence": 67,
                "analysis": "该线索的置信度约 70%，建议继续跟进。",
                "items": [{"purchase_probability": "0.42"}],
            }
        }
    ]

    violations = NoProbabilityOutputRail().check(change_set)

    assert [violation.location for violation in violations] == [
        "changes[0].payload.confidence",
        "changes[0].payload.analysis",
        "changes[0].payload.items[0].purchase_probability",
    ]
    assert all(violation.rail == "no_probability_output" for violation in violations)


def test_probability_rail_allows_discrete_evidence_labels() -> None:
    change_set = _change_set()
    change_set.changes = [
        {
            "payload": {
                "confidence": "strong",
                "evidence_level": "customer_confirmed",
                "analysis": "客户已明确确认材质和数量。",
            }
        }
    ]

    assert NoProbabilityOutputRail().check(change_set) == []


def test_evidence_rail_reports_missing_phase1_provenance_at_exact_paths() -> None:
    change_set = _change_set()
    change_set.changes = [
        {
            "domain": "demand",
            "operation": "capture_signal",
            "payload": {
                "source_type": "web_page",
                "source_id": "",
                "source_url": "https://buyer.example/news",
                "page_hash": "a" * 64,
                "snapshot_artifact_ref": "artifact-one",
                "observed_at": "2026-08-21T10:00:00+00:00",
                "extracted_by": "model-v1",
            },
        },
        {
            "domain": "demand",
            "operation": "create_hypothesis",
            "payload": {
                "signal_indexes": [],
                "evidence_levels": [],
                "inferred_by": "model-v1",
            },
        },
        {
            "domain": "demand",
            "operation": "update_need_fields",
            "payload": {
                "message_id": "message-one",
                "fields": [{"field": "quantity", "value": "5000", "quote": " "}],
            },
        },
        {
            "domain": "prospecting",
            "operation": "resolve_account",
            "payload": {"source_signal_refs": []},
        },
    ]

    violations = EvidenceRequiredRail().check(change_set)

    assert [violation.location for violation in violations] == [
        "changes[0].payload.source_id",
        "changes[1].payload.signal_indexes",
        "changes[1].payload.evidence_levels",
        "changes[2].payload.fields[0].quote",
        "changes[3].payload.source_signal_refs",
    ]


def test_evidence_rail_accepts_complete_phase1_evidence_contracts() -> None:
    change_set = _change_set()
    change_set.changes = [
        {
            "domain": "demand",
            "operation": "capture_signal",
            "payload": {
                "source_type": "web_page",
                "source_id": "a" * 64,
                "source_url": "https://buyer.example/news",
                "page_hash": "a" * 64,
                "snapshot_artifact_ref": "artifact-one",
                "observed_at": "2026-08-21T10:00:00+00:00",
                "extracted_by": "model-v1",
            },
        },
        {
            "domain": "demand",
            "operation": "create_hypothesis",
            "payload": {
                "signal_indexes": [0],
                "evidence_levels": ["public_company_event"],
                "inferred_by": "model-v1",
            },
        },
        {
            "domain": "demand",
            "operation": "update_need_fields",
            "payload": {
                "message_id": "message-one",
                "fields": [
                    {
                        "field": "quantity",
                        "value": "5000",
                        "quote": "We need 5000 units.",
                    }
                ],
            },
        },
        {
            "domain": "prospecting",
            "operation": "resolve_account",
            "payload": {"source_signal_refs": ["signal-one"]},
        },
    ]

    assert EvidenceRequiredRail().check(change_set) == []
