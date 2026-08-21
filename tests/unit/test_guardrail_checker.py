from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.base import ChangeSet
from agent_runtime.guardrails.rails import (
    GuardrailChecker,
    RailViolation,
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
