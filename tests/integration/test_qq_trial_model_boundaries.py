"""可控到期/取消下的确定性阶段语义；真实一秒计时由 early_timeout 模块独立验证。"""

import asyncio
import json

import pytest
from sqlalchemy import select

from infra.db.qq_trial import SqlQQTrialGate
from infra.db.tables import (
    ModelInvocationRow,
    ModelQuotaBucketRow,
    ToolCallEventRow,
    ToolCallRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.model_invocation import ModelGenerationError
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration.qq_model_timing_probe import TimingProbe
from tests.integration.test_qq_trial_ledger import received, snapshot
from tests.integration.test_qq_trial_ledger import trial as trial  # noqa: PLC0414
from tests.integration.test_qq_trial_model import FakeSDK, assemble
from tool_gateway.repository import ToolCallId


async def ledgers(gate, sessions):
    tenant = gate.scope.route.tenant_id
    row = await snapshot(gate, sessions)
    async with sessions() as session:
        calls = (
            await session.scalars(
                select(ToolCallRow)
                .where(
                    ToolCallRow.tenant_id == tenant,
                    ToolCallRow.tool_id == "model.generate",
                )
                .order_by(ToolCallRow.created_at)
            )
        ).all()
        ids = [r.tool_call_id for r in calls]
        events = (
            await session.scalars(
                select(ToolCallEventRow)
                .where(
                    ToolCallEventRow.tenant_id == tenant,
                    ToolCallEventRow.tool_call_id.in_(ids),
                )
                .order_by(ToolCallEventRow.occurred_at, ToolCallEventRow.event_id)
            )
        ).all()
        usage = (
            await session.scalars(
                select(ModelInvocationRow).where(ModelInvocationRow.tenant_id == tenant)
            )
        ).all()
        buckets = (
            await session.scalars(
                select(ModelQuotaBucketRow).where(
                    ModelQuotaBucketRow.tenant_id == tenant
                )
            )
        ).all()
    return {
        "trial": {
            k: getattr(row, k)
            for k in (
                "state",
                "reason",
                "classifications",
                "provider_requests",
                "provider_pending",
                "input_bytes",
                "output_tokens",
                "bodies",
            )
        },
        "calls": [
            {
                "id": r.tool_call_id,
                "status": r.status,
                "category": r.error_category,
                "attempts": r.attempt_count,
                "leased": r.lease_owner is not None,
                "retry_after": r.retry_after_at is not None,
                "completed": r.completed_at is not None,
                "provider_ref": r.provider_ref,
            }
            for r in calls
        ],
        "events": [(r.stage, r.outcome, r.rule, r.category) for r in events],
        "usage": [
            (r.state, r.slot_released, r.input_tokens, r.output_tokens) for r in usage
        ],
        "buckets": sorted(
            (
                "employee" if r.scope_key.startswith("employee:") else r.scope_key,
                r.calls,
            )
            for r in buckets
        ),
    }


async def run_at_phase(
    trial, phase, mode, *, monkeypatch, revoke=False, case_name=None
):
    gate, sessions, *rest = trial
    await received(gate, sessions)
    probe = TimingProbe(clock_kind="controlled_expiry")
    probe.install(monkeypatch)
    entered = asyncio.Event()
    reached = []
    original_reserve = gate.reserve

    async def hold(name):
        reached.append(name)
        probe.mark("phase:" + name)
        entered.set()
        await asyncio.Event().wait()

    async def reserve(kind, *, input_bytes=0):
        if kind == "provider_requests" and phase == "before_provider_reserve":
            await hold(phase)
        await original_reserve(kind, input_bytes=input_bytes)
        if kind == "provider_requests" and phase == "after_provider_commit":
            await hold(phase)

    class SDK(FakeSDK):
        async def create(self, **body):
            assert phase == "sdk_inflight"
            if mode == "unknown":
                reached.append(phase)
                return await super().create(**body)
            self.requests.append(body)
            self.started.set()
            await hold(phase)

    gate.reserve = reserve
    sdk = SDK("unknown" if mode == "unknown" else "slow")
    generator, actor, request = assemble(gate, sessions, sdk, timeout_seconds=1)
    assert generator.ports.limits.timeout_seconds == 1
    task = asyncio.create_task(generator.generate(actor, request))
    observed = "not_completed"
    try:
        if mode in {"cancel", "timeout"}:
            # 只限制测试自身等待时间，不替代产品限时或预算。
            await asyncio.wait_for(entered.wait(), 10)
            if revoke:
                # 夹具当前授权字典，不替换受信 Gate 或撤销检查。
                state = next(
                    item
                    for item in rest
                    if isinstance(item, dict) and "model_permission" in item
                )
                state["model_permission"] = False
            if mode == "cancel":
                task.cancel()
            else:
                probe.expire()
        expected = {
            "cancel": asyncio.CancelledError,
            "timeout": TimeoutError,
            "unknown": ModelGenerationError,
        }[mode]
        with pytest.raises(expected):
            await task
        observed = expected.__name__
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        report = await probe.capture(
            case_name or (phase + "-" + mode + ("-revoked" if revoke else "")),
            gate,
            sessions,
            sdk,
            outcome=observed,
            reached=reached,
        )
    assert "snapshot_unavailable" not in report
    assert probe.timeouts == [1]
    assert probe.expiry_injected == (mode == "timeout")
    assert reached == [phase], "没有到达声明阶段，不能把提前超时当成本场景通过"
    return gate, sessions, sdk, generator, actor, request


async def assert_stopped_without_replay(context, phase, mode, *, cleanup_failed=False):
    gate, sessions, sdk, generator, actor, request = context
    before = await ledgers(gate, sessions)
    reserved = int(phase != "before_provider_reserve")
    sdk_calls = int(phase == "sdk_inflight")
    row = before["trial"]
    assert row == {
        "state": "stopped",
        "reason": "failed_or_unknown",
        "classifications": 1,
        "provider_requests": reserved,
        "provider_pending": bool(reserved),
        "input_bytes": 421 if reserved else 0,
        "output_tokens": 0,
        "bodies": 1,
    }
    assert generator.provider_budget.completed == 0 and len(sdk.requests) == sdk_calls
    assert before["usage"] == [("unknown", False, None, None)]
    assert before["buckets"] == [("employee", 1), ("tenant", 1)]
    assert len(before["calls"]) == 1
    call = before["calls"][0]
    assert call == {
        "id": call["id"],
        "status": "executing" if cleanup_failed else "failed_permanent",
        "category": None if cleanup_failed else "reconciliation_required",
        "attempts": 1,
        "leased": cleanup_failed,
        "retry_after": False,
        "completed": not cleanup_failed,
        "provider_ref": None,
    }
    if not cleanup_failed:
        if mode == "unknown":
            assert ("connector", "failed", None, "reconciliation_required") in before[
                "events"
            ]
        else:
            assert (
                "runtime",
                "cancelled",
                "cancellation:manual_reconciliation",
                "reconciliation_required",
            ) in before["events"]
    # 不同租户无法看到本调用，收尾不获得跨租户读取权。
    other = TenantId(new_id("tn"))
    async with SqlAlchemyToolGatewayUnitOfWork(sessions, other) as uow:
        assert await uow.calls.get(other, ToolCallId(call["id"])) is None
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
    assert len(sdk.requests) == sdk_calls
    print(
        json.dumps(
            {
                "phase": phase,
                "mode": mode,
                "cleanup_failed": cleanup_failed,
                "ledger": before,
                "sdk_calls": sdk_calls,
            },
            ensure_ascii=True,
        )
    )


@pytest.mark.parametrize(
    "phase", ["before_provider_reserve", "after_provider_commit", "sdk_inflight"]
)
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_controlled_expiry_or_cancel_at_proven_phase_stops_without_replay(
    trial, monkeypatch, phase, mode
):
    await assert_stopped_without_replay(
        await run_at_phase(trial, phase, mode, monkeypatch=monkeypatch), phase, mode
    )


async def test_unknown_sdk_result_is_terminal_and_auditable(trial, monkeypatch):
    await assert_stopped_without_replay(
        await run_at_phase(trial, "sdk_inflight", "unknown", monkeypatch=monkeypatch),
        "sdk_inflight",
        "unknown",
    )


async def test_revoked_permission_cannot_prevent_cleanup_or_enable_replay(
    trial, monkeypatch
):
    await assert_stopped_without_replay(
        await run_at_phase(
            trial, "sdk_inflight", "cancel", monkeypatch=monkeypatch, revoke=True
        ),
        "sdk_inflight",
        "cancel",
    )


async def test_cleanup_audit_failure_preserves_evidence_and_original_cancellation(
    trial, monkeypatch, caplog
):
    from infra.db.repositories.tool_calls import ToolCallRepositoryImpl

    original = ToolCallRepositoryImpl.append_event

    async def append(self, event):
        if event.rule == "cancellation:manual_reconciliation":
            raise RuntimeError("PRIVATE_CLEANUP_FAILURE")
        await original(self, event)

    monkeypatch.setattr(ToolCallRepositoryImpl, "append_event", append)
    await assert_stopped_without_replay(
        await run_at_phase(
            trial,
            "sdk_inflight",
            "cancel",
            monkeypatch=monkeypatch,
            case_name="sdk-cancel-audit-failure",
        ),
        "sdk_inflight",
        "cancel",
        cleanup_failed=True,
    )
    assert "工具取消状态写入未确认" in caplog.text
    assert "PRIVATE_CLEANUP_FAILURE" not in caplog.text


async def test_current_permission_and_identity_scope_still_gate_model_calls(trial):
    gate, sessions, _, state, *_ = trial
    await received(gate, sessions)
    sdk = FakeSDK()
    generator, actor, request = assemble(gate, sessions, sdk, timeout_seconds=1)
    for field, prefix in (
        ("tenant_id", "tn"),
        ("employee_id", "emp"),
        ("user_id", "usr"),
    ):
        with pytest.raises(QQTrialStopped):
            await generator.generate(
                actor.model_copy(update={field: new_id(prefix)}), request
            )
    state["model_permission"] = False
    with pytest.raises(QQTrialStopped):
        await generator.generate(actor, request)
    result = await ledgers(gate, sessions)
    assert result["trial"]["classifications"] == 0
    assert result["trial"]["provider_requests"] == 0
    assert result["calls"] == result["usage"] == result["buckets"] == []
    assert sdk.requests == []
