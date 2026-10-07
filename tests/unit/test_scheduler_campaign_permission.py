"""Scheduler 发送权限与真实触达暂态失败状态的组合回归。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.scheduler_worker.campaign_driver import SchedulerCampaignPermissionCheck
from domains.outreach.schemas import MessageAttemptState, SendFailureCategory
from domains.sending_identity.errors import (
    AuthenticationNotVerifiedError,
    WarmupLimitExceededError,
)
from shared.errors import TransientError
from shared.schemas.identifiers import UserId, new_id
from tests.outreach_fakes import FakeUowFactory
from tests.unit.test_outreach_send_claim import _prepared_harness
from tests.unit.test_tool_gateway_pipeline import _tool_manifest
from tool_gateway.checks.rate_limit import RateLimitCheck
from tool_gateway.errors import ToolGatewayError
from tool_gateway.pipeline import ToolCallContext, ToolInvocationState


@pytest.mark.parametrize(
    "error",
    [
        WarmupLimitExceededError("synthetic quota exhausted"),
        AuthenticationNotVerifiedError("synthetic authentication unavailable"),
        TransientError("synthetic reservation unavailable"),
    ],
)
async def test_scheduler_retries_same_attempt_after_pre_dispatch_transient_failure(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    harness, actor, attempt = await _prepared_harness()
    factory = FakeUowFactory(harness.store, harness.trace)
    monkeypatch.setattr(
        "apps.scheduler_worker.campaign_driver.SqlAlchemyOutreachUnitOfWork",
        lambda _factory, tenant: factory(tenant),
    )
    checker = SchedulerCampaignPermissionCheck(None, harness.tenant)
    ctx = ToolCallContext(
        tenant_id=harness.tenant,
        user_id=UserId(new_id("usr")),
        tool_id="email.send",
        params={
            "attempt_id": str(attempt.attempt_id),
            "enrollment_id": str(attempt.enrollment_id),
            "subject": "Hello",
            "body": "Could you share your sourcing needs?",
        },
        idempotency_key=attempt.idempotency_key,
        campaign_ref=str(attempt.campaign_id),
    )

    class SlotReservation:
        failure = error

        async def reserve_send_slot(self, *args, **kwargs):
            if self.failure is not None:
                raise self.failure

    slots = SlotReservation()
    rate = RateLimitCheck(
        harness.service, slots, lambda _ctx: actor, lambda _ctx, _preflight: object()
    )
    state = ToolInvocationState(manifest=_tool_manifest(), tool_call_id=new_id("tcl"))
    state.preflight = await harness.service.preflight_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    assert await checker.authorize(ctx, state)
    with pytest.raises(ToolGatewayError):
        await rate.check(ctx, state)
    assert harness.store.attempts[attempt.attempt_id].state is MessageAttemptState.FAILED_TRANSIENT

    prepared_again = await harness.service.prepare_message_attempt(
        harness.tenant, attempt.enrollment_id, actor=actor
    )
    assert prepared_again.attempt_id == attempt.attempt_id
    assert prepared_again.idempotency_key == attempt.idempotency_key
    state.preflight = await harness.service.preflight_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    assert await checker.authorize(ctx, state)
    assert not await checker.authorize(replace(ctx, idempotency_key="different-key"), state)
    slots.failure = None
    assert await rate.check(ctx, state) is None
    assert harness.store.attempts[attempt.attempt_id].state is MessageAttemptState.SENDING
    await harness.service.record_sent(
        harness.tenant, attempt.attempt_id, "provider_ref_1", actor=actor
    )
    assert len(harness.store.attempts) == 1
    assert len(harness.store.events) == 1


async def test_scheduler_rejects_permanently_failed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, actor, attempt = await _prepared_harness()
    await harness.service.claim_message_send(harness.tenant, attempt.attempt_id, actor=actor)
    await harness.service.record_send_failure(
        harness.tenant, attempt.attempt_id, SendFailureCategory.PROVIDER_PERMANENT,
        actor=actor,
    )
    factory = FakeUowFactory(harness.store, harness.trace)
    monkeypatch.setattr(
        "apps.scheduler_worker.campaign_driver.SqlAlchemyOutreachUnitOfWork",
        lambda _factory, tenant: factory(tenant),
    )
    ctx = ToolCallContext(
        tenant_id=harness.tenant,
        user_id=UserId(new_id("usr")),
        tool_id="email.send",
        params={"attempt_id": str(attempt.attempt_id), "enrollment_id": str(attempt.enrollment_id)},
        idempotency_key=attempt.idempotency_key,
        campaign_ref=str(attempt.campaign_id),
    )
    assert not await SchedulerCampaignPermissionCheck(None, harness.tenant).authorize(ctx, None)
