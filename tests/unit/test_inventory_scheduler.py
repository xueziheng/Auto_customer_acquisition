"""候选采集只在原 scheduler 单实例锁有效期间执行。"""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from apps.scheduler_worker.main import SchedulerConfig, SchedulerRuntime, _run_cycle
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId


def runtime(events, *, driver):
    class Outbox:
        async def drain(self):
            events.append("outbox")
            return 0

    class Workflow:
        async def poll_due(self, tenant, limit):
            events.append("workflow")
            return 1

    return SchedulerRuntime(
        lock_engine=Mock(), outbox=Outbox(), workflow=Workflow(),
        tenant_id=TenantId("tn_inventory_scheduler"),
        config=SchedulerConfig(interval_seconds=1, batch_limit=1, lock_key=51),
        inventory_driver=driver,
    )


class Driver:
    def __init__(self, events, error=None):
        self.events, self.error = events, error

    async def scan(self):
        self.events.append("inventory")
        if self.error is not None:
            raise self.error
        return 1


@pytest.mark.asyncio
async def test_inventory_is_guarded_before_after_and_before_post_outbox():
    events = []
    async def guard():
        events.append("lock")
    await _run_cycle(runtime(events, driver=Driver(events)), 1, confirm_lock=guard)
    assert events == ["outbox", "lock", "inventory", "lock", "workflow", "lock", "outbox"]


@pytest.mark.asyncio
async def test_inventory_requires_guard_before_any_side_effect():
    events = []
    with pytest.raises(RuntimeError):
        await _run_cycle(runtime(events, driver=Driver(events)), 1)
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [1, 2, 3])
async def test_inventory_lost_lock_prevents_later_phases(fail_at):
    events, calls = [], 0
    async def guard():
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise RuntimeError("lock lost")
    with pytest.raises(RuntimeError, match="lock lost"):
        await _run_cycle(runtime(events, driver=Driver(events)), 1, confirm_lock=guard)
    assert events == ["outbox"] + (["inventory"] if fail_at > 1 else []) + (["workflow"] if fail_at > 2 else [])


@pytest.mark.asyncio
async def test_inventory_failure_does_not_leak_external_exception(caplog):
    events = []
    async def guard():
        pass
    await _run_cycle(runtime(events, driver=Driver(events, ValueError("SENSITIVE_PROVIDER_BODY"))), 1, confirm_lock=guard)
    assert "workflow" in events
    assert "SENSITIVE_PROVIDER_BODY" not in caplog.text
    assert any(getattr(r, "scheduler_phase", None) == "inventory" for r in caplog.records)


@pytest.mark.asyncio
async def test_inventory_cancellation_prevents_other_phases():
    events = []
    async def guard():
        pass
    with pytest.raises(asyncio.CancelledError):
        await _run_cycle(runtime(events, driver=Driver(events, asyncio.CancelledError())), 1, confirm_lock=guard)
    assert events == ["outbox", "inventory"]


def test_invalid_inventory_driver_rejected_at_construction():
    with pytest.raises(ValidationError):
        runtime([], driver=object())
