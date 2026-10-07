"""限速技术预算/互斥结果及claim后注入，不伪称持久验收。"""

import pytest
from pydantic import ValidationError

from tests.unit.test_quote_file_gateway import snapshot
from tests.unit.test_quote_pdf_artifacts import ULID
from tool_gateway.checks.quote_files import QuoteFileRateLimitCheck
from tool_gateway.file_rate_limit import (
    QuoteFileRateDecision,
    QuoteFileRateLimits,
)
from tool_gateway.handlers.quote_files import (
    GENERATE_MANIFEST,
    QuoteFilePrepared,
    QuoteFileResultSlot,
    quote_file_generation_key,
)
from tool_gateway.pipeline import PreparedToolCall, ToolCallContext, ToolInvocationState


@pytest.mark.parametrize("field", QuoteFileRateLimits.model_fields)
@pytest.mark.parametrize("value", [0, -1, True, "2", None])
def test_limits_require_explicit_positive_ints(field, value):
    values = {
        "maximum_admissions": 2,
        "window_seconds": 60,
        "lock_timeout_ms": 100,
        "statement_timeout_ms": 500,
    }
    values[field] = value
    with pytest.raises(ValidationError):
        QuoteFileRateLimits(**values)


@pytest.mark.parametrize(
    "values",
    [
        {
            "outcome": "reserved",
            "reservation_event_id": None,
            "retry_after_seconds": None,
        },
        {
            "outcome": "limited",
            "reservation_event_id": f"tce_{ULID}",
            "retry_after_seconds": 1,
        },
        {
            "outcome": "limited",
            "reservation_event_id": None,
            "retry_after_seconds": True,
        },
        {
            "outcome": "limited",
            "reservation_event_id": None,
            "retry_after_seconds": 86401,
        },
    ],
)
def test_rate_decision_cannot_fake_reservation(values):
    with pytest.raises(ValidationError):
        QuoteFileRateDecision(**values)


async def test_rate_check_uses_canonical_state_and_current_actor():
    s = snapshot()

    class Rate:
        async def reserve(self, tenant, request):
            assert tenant == s.tenant_id
            assert request.canonical_call_id == f"tcl_{ULID}"
            assert request.actor_id == "emp_manager" and request.tool_version == "v1"
            return QuoteFileRateDecision(
                outcome="reserved",
                reservation_event_id=f"tce_{ULID}",
                retry_after_seconds=None,
            )

    ctx = ToolCallContext(
        s.tenant_id,
        "emp_manager",
        "quotation.file.generate",
        {"quote_id": s.quote_id},
        idempotency_key=quote_file_generation_key(s),
    )
    payload = QuoteFilePrepared(
        tenant_id=s.tenant_id,
        actor_id="emp_manager",
        tool_id=ctx.tool_id,
        quote_id=s.quote_id,
        file_id=None,
        snapshot=s,
        history_file=None,
        rate_claim=None,
    )
    prepared = PreparedToolCall("a" * 64, "test-v1", {}, payload)
    state = ToolInvocationState(GENERATE_MANIFEST, f"tcl_{ULID}", prepared=prepared)
    assert (
        await QuoteFileRateLimitCheck(Rate(), QuoteFileResultSlot()).check(ctx, state)
        is None
    )
    assert state.prepared.request_fingerprint == prepared.request_fingerprint
    assert state.prepared.payload.rate_claim.actor_id == "emp_manager"
    assert prepared.payload.rate_claim is None
