"""恢复前置条件必须依据持久事实；不能把未知派发当作可重试。"""

from dataclasses import replace

import pytest

from shared.errors import ValidationError
from workflows.reply_qualification.retry import RetryFacts, require_retryable


def facts():
    return RetryFacts(
        run_status="failed",
        step="classify",
        invocation_count=1,
        invocation_state="rejected",
        dispatched=False,
        sequence=0,
        classified=False,
        provider_requests=0,
        provider_pending=False,
        configuration_matches=True,
        original_binding=True,
    )


def test_definitely_not_dispatched_can_retry():
    require_retryable(facts())


@pytest.mark.parametrize(
    "change",
    [
        {"run_status": "running"},
        {"step": "apply_actions"},
        {"invocation_count": 0},
        {"invocation_count": 2},
        {"invocation_state": "unknown"},
        {"invocation_state": "succeeded"},
        {"dispatched": True},
        {"sequence": 1},
        {"classified": True},
        {"provider_requests": 1},
        {"provider_pending": True},
        {"configuration_matches": False},
        {"original_binding": False},
    ],
)
def test_ambiguous_or_changed_facts_are_not_recoverable(change):
    with pytest.raises(ValidationError):
        require_retryable(replace(facts(), **change))
