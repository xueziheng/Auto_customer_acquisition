"""一次有界诊断组；保留原一秒计时与正式阶段前提，失败也输出证据。"""

import pytest

from tests.integration.qq_model_timing_probe import TimingProbe
from tests.integration.test_qq_trial_ledger import received
from tests.integration.test_qq_trial_ledger import trial as trial  # noqa: PLC0414
from tests.integration.test_qq_trial_model import FakeSDK, assemble


@pytest.mark.parametrize(
    "block",
    [
        "before_provider_reserve",
        "after_received_commit",
        "after_claim_commit",
        "after_quota_commit",
        "after_executing_commit",
    ],
)
async def test_one_second_stage_diagnostic(trial, monkeypatch, block):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    probe = TimingProbe(block=block)
    probe.install(monkeypatch)
    reserve = gate.reserve
    reached = []

    async def traced_reserve(kind, *, input_bytes=0):
        probe.mark("trial.reserve.enter:" + kind)
        if kind == "provider_requests":
            reached.append("before_provider_reserve")
            await probe.hold("before_provider_reserve")
        await reserve(kind, input_bytes=input_bytes)
        probe.mark("trial.reserve.returned:" + kind)

    gate.reserve = traced_reserve
    sdk = FakeSDK("slow")
    generator, actor, request = assemble(gate, sessions, sdk, timeout_seconds=1)
    observed = "no_result"
    try:
        await generator.generate(actor, request)
        observed = "returned"
    except TimeoutError:
        observed = "TimeoutError"
    finally:
        result = await probe.capture(
            "diagnostic-" + block,
            gate,
            sessions,
            sdk,
            outcome=observed,
            reached=reached,
        )
    assert "snapshot_unavailable" not in result
    assert observed == "TimeoutError" and probe.timeouts == [1]
    assert probe.block_reached, "未到注入阶段，不得把提前超时算成该阶段通过"
    assert result["ledger"]["trial"]["state"] == "stopped"
    assert sdk.requests == []
