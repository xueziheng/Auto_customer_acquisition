"""到期驱动只委托既有域；真实singleton/到期状态另经PG验证。"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.scheduler_worker import main as scheduler


def driver_type():
    assert importlib.util.find_spec("workflows.quote_approval.expiry"), (
        "缺少报价到期驱动"
    )
    return importlib.import_module("workflows.quote_approval.expiry").QuoteExpiryDriver


async def test_expiry_driver_delegates_exact_tenant_and_explicit_limit_once():
    service = AsyncMock()
    service.expire_overdue.return_value = 3
    driver = driver_type()(service, "tenant", limit=17)
    assert await driver.scan_once() == 3
    service.expire_overdue.assert_awaited_once_with("tenant", limit=17)
    assert len(service.mock_calls) == 1


@pytest.mark.parametrize(
    "failure", [RuntimeError("private failure"), asyncio.CancelledError()]
)
async def test_expiry_driver_preserves_domain_failure_without_retry(failure):
    service = AsyncMock()
    service.expire_overdue.side_effect = failure
    driver = driver_type()(service, "tenant", limit=1)
    with pytest.raises(type(failure)) as error:
        await driver.scan_once()
    assert error.value is failure
    assert service.expire_overdue.await_count == 1


@pytest.mark.parametrize("failure", [None, "error", "cancel"])
@pytest.mark.parametrize("progress", [0, 1])
async def test_cycle_places_expiry_after_campaign_before_workflow_and_isolates_errors(
    failure, progress, caplog
):
    assert "quote_expiry_driver" in scheduler.SchedulerRuntime.__dataclass_fields__, (
        "实际runtime未接到期驱动"
    )
    calls = []

    class Outbox:
        async def drain(self):
            calls.append("outbox")
            return 0

    class Campaign:
        async def scan_once(self):
            calls.append("campaign")
            return 0

    class Expiry:
        async def scan_once(self):
            calls.append("expiry")
            if failure == "error":
                raise RuntimeError("private expiry failure")
            if failure == "cancel":
                raise asyncio.CancelledError()
            return 10

    class Workflow:
        async def poll_due(self, tenant, limit):
            calls.append("workflow")
            return progress

    runtime = scheduler.SchedulerRuntime(
        SimpleNamespace(),
        Outbox(),
        Workflow(),
        "tenant",
        scheduler.SchedulerConfig(1, 5, 1),
        campaign_driver=Campaign(),
        quote_expiry_driver=Expiry(),
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await scheduler._run_cycle(runtime, 1)
        assert calls == ["outbox", "campaign", "expiry"]
    else:
        await scheduler._run_cycle(runtime, 1)
        assert calls == ["outbox", "campaign", "expiry", "workflow"] + (
            ["outbox"] if progress else []
        )
        if failure == "error":
            assert any(
                getattr(r, "scheduler_phase", None) == "quote_expiry"
                for r in caplog.records
            )
    assert "private expiry failure" not in caplog.text
