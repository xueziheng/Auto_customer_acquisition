"""真实一秒端到端检查与确定性早期分支分开；未到声明阶段只记早期覆盖。"""

import asyncio

import pytest

from infra.db.qq_trial import SqlQQTrialGate
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration.qq_model_timing_probe import TimingProbe
from tests.integration.test_qq_trial_ledger import received
from tests.integration.test_qq_trial_ledger import trial as trial  # noqa: PLC0414
from tests.integration.test_qq_trial_model import FakeSDK, assemble
from tests.integration.test_qq_trial_model_boundaries import ledgers


async def observe(trial, monkeypatch, *, clock_kind, block):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    probe = TimingProbe(clock_kind=clock_kind, block=block)
    probe.install(monkeypatch)
    reserve = gate.reserve
    reached = []

    async def guarded_reserve(kind, *, input_bytes=0):
        if kind == "provider_requests":
            reached.append("before_provider_reserve")
            await probe.hold("before_provider_reserve")
        await reserve(kind, input_bytes=input_bytes)

    gate.reserve = guarded_reserve
    sdk = FakeSDK("slow")
    generator, actor, request = assemble(gate, sessions, sdk, timeout_seconds=1)
    task = asyncio.create_task(generator.generate(actor, request))
    observed = "not_completed"
    try:
        if clock_kind == "controlled_expiry":
            await asyncio.wait_for(probe.blocked.wait(), 10)
            probe.expire()
        with pytest.raises(TimeoutError):
            await task
        observed = "TimeoutError"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        probe.mark(
            "coverage.declared_phase" if reached else "coverage.early_before_phase"
        )
        report = await probe.capture(
            clock_kind + "-" + block,
            gate,
            sessions,
            sdk,
            outcome=observed,
            reached=reached,
        )
    assert "snapshot_unavailable" not in report
    assert probe.timeouts == [1]
    assert generator.ports.limits.timeout_seconds == 1
    assert observed == "TimeoutError"
    return (gate, sessions, sdk, generator, actor, request), probe, report


async def assert_safe_stop_and_no_replay(context, report):
    gate, sessions, sdk, generator, actor, request = context
    before = report["ledger"]
    assert before["trial"] == {
        "state": "stopped",
        "reason": "failed_or_unknown",
        "classifications": 1,
        "provider_requests": 0,
        "provider_pending": False,
        "input_bytes": 0,
        "output_tokens": 0,
        "bodies": 1,
    }
    assert sdk.requests == [] and generator.provider_budget.completed == 0
    assert len(before["calls"]) <= 1
    for call in before["calls"]:
        assert call["provider_ref"] is None and not call["retry_after"]
        if call["status"] in {"received", "claimed"}:
            assert call["category"] is None and not call["completed"]
            assert call["attempts"] == int(call["status"] == "claimed")
            assert call["leased"] == (call["status"] == "claimed")
        else:
            assert call["status"] == "failed_permanent"
            assert call["category"] == "reconciliation_required"
            assert call["attempts"] == 1 and call["completed"] and not call["leased"]
            assert (
                "runtime",
                "cancelled",
                "cancellation:manual_reconciliation",
                "reconciliation_required",
            ) in before["events"]
    assert len(before["usage"]) <= 1
    for state, released, inputs, outputs in before["usage"]:
        assert state in {"reserved", "unknown", "rejected"}
        assert released == (state == "rejected")
        assert inputs is None and outputs is None
    assert before["buckets"] == (
        [("employee", 1), ("tenant", 1)] if before["usage"] else []
    )
    for candidate in (
        generator,
        assemble(
            SqlQQTrialGate(sessions, gate.scope, gate.authorize),
            sessions,
            sdk,
            timeout_seconds=1,
        )[0],
    ):
        with pytest.raises(QQTrialStopped):
            await candidate.generate(actor, request)
    assert await ledgers(gate, sessions) == before
    assert sdk.requests == []


async def test_real_one_second_timeout_records_declared_or_early_branch(
    trial, monkeypatch
):
    context, probe, report = await observe(
        trial,
        monkeypatch,
        clock_kind="wall_clock",
        block="before_provider_reserve",
    )
    await assert_safe_stop_and_no_replay(context, report)
    starts = [e for e in probe.events if e["stage"] == "deadline.enter"]
    exits = [e for e in probe.events if e["stage"] == "deadline.exit"]
    assert len(starts) == len(exits) == 1 and exits[0]["expired"]
    start = starts[0]
    assert start["created_before_ms"] + 999.998 <= start["deadline_ms"]
    assert start["deadline_ms"] <= start["created_after_ms"] + 1000.002
    assert not probe.expiry_injected
    if report["reached"]:
        assert report["reached"] == ["before_provider_reserve"]
        assert report["ledger"]["calls"][0]["status"] == "failed_permanent"
        assert report["ledger"]["usage"] == [("unknown", False, None, None)]
    else:
        # 只证明实际早期分支已停止；绝不算作到达 provider 阶段。
        assert not probe.block_reached
        assert any(e["stage"] == "coverage.early_before_phase" for e in probe.events)


@pytest.mark.parametrize(
    "block,status,usage",
    [
        ("after_received_commit", "received", []),
        ("after_claim_commit", "claimed", []),
        ("after_quota_commit", "claimed", [("reserved", False, None, None)]),
        (
            "after_executing_commit",
            "failed_permanent",
            [("reserved", False, None, None)],
        ),
    ],
)
async def test_controlled_early_expiry_preserves_committed_state(
    trial,
    monkeypatch,
    block,
    status,
    usage,
):
    context, probe, report = await observe(
        trial,
        monkeypatch,
        clock_kind="controlled_expiry",
        block=block,
    )
    assert probe.block_reached and probe.expiry_injected
    assert report["reached"] == []
    assert len(report["ledger"]["calls"]) == 1
    assert report["ledger"]["calls"][0]["status"] == status
    assert report["ledger"]["usage"] == usage
    await assert_safe_stop_and_no_replay(context, report)
