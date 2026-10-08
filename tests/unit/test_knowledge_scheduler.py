"""企业资料扫描只在原 scheduler 单副本边界内运行。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from apps.scheduler_worker.main import _run_cycle
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

    class Assistant:
        async def scan_once(self):
            events.append("assistant")
            return 1

    return SimpleNamespace(
        outbox=Outbox(), workflow=Workflow(), tenant_id=TenantId("tn_test"),
        config=SimpleNamespace(batch_limit=2), campaign_driver=None,
        quote_expiry_driver=None, sourcing_admission_driver=None,
        catalog_product_driver=None, inbound_driver=None, knowledge_driver=driver,
        assistant_driver=Assistant(),
    )


@pytest.mark.asyncio
async def test_knowledge_scan_is_between_lock_checks_before_assistant():
    events = []
    class Driver:
        async def scan_once(self):
            events.append("knowledge")
            return 1
    async def guard():
        events.append("lock")
    await _run_cycle(runtime(events, driver=Driver()), 1, confirm_lock=guard)
    assert events == ["outbox", "lock", "knowledge", "lock", "lock", "assistant", "lock", "workflow", "lock", "outbox"]


@pytest.mark.asyncio
async def test_missing_lock_guard_rejects_before_any_phase():
    events = []
    class Driver:
        async def scan_once(self):
            events.append("knowledge")
            return 0
    with pytest.raises(RuntimeError):
        await _run_cycle(runtime(events, driver=Driver()), 1)
    assert events == []


@pytest.mark.asyncio
async def test_lost_lock_after_knowledge_prevents_later_phases():
    events = []
    class Driver:
        async def scan_once(self):
            events.append("knowledge")
            return 1
    async def guard():
        if "knowledge" in events:
            raise RuntimeError("lock lost")
    with pytest.raises(RuntimeError, match="lock lost"):
        await _run_cycle(runtime(events, driver=Driver()), 1, confirm_lock=guard)
    assert events == ["outbox", "knowledge"]


@pytest.mark.asyncio
async def test_knowledge_failure_is_fixed_phase_and_does_not_leak_original(caplog):
    events = []
    class Driver:
        async def scan_once(self):
            raise ValueError("SENSITIVE_DOCUMENT_BODY")
    async def guard():
        pass
    await _run_cycle(runtime(events, driver=Driver()), 1, confirm_lock=guard)
    assert "assistant" in events
    assert "SENSITIVE_DOCUMENT_BODY" not in caplog.text
    assert any(getattr(record, "scheduler_phase", None) == "enterprise_knowledge" for record in caplog.records)


@pytest.mark.asyncio
async def test_knowledge_cancellation_propagates_without_later_phases():
    events = []
    class Driver:
        async def scan_once(self):
            raise asyncio.CancelledError()
    async def guard():
        pass
    with pytest.raises(asyncio.CancelledError):
        await _run_cycle(runtime(events, driver=Driver()), 1, confirm_lock=guard)
    assert events == ["outbox"]
