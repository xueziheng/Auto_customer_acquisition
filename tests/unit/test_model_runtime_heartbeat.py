"""长任务期间持续心跳；单副本锁失效/关闭后停止，后台心跳不执行恢复扫描。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from infra.standalone.runtime import ModelRuntimeLifecycle
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId
from tests.unit.test_standalone_model_settings import settings


async def test_scheduler_heartbeat_continues_during_work_and_stops_on_lock_loss():
    repo = SimpleNamespace(
        initialize=AsyncMock(),
        register_process=AsyncMock(),
        heartbeat=AsyncMock(),
        unregister_process=AsyncMock(),
    )
    recovery = AsyncMock()
    lifecycle = ModelRuntimeLifecycle(
        repo,
        StandaloneModelSettings.model_validate(settings()),
        TenantId("tn_test"),
        "scheduler",
        recovery=recovery,
    )
    lifecycle.heartbeat_interval_seconds = 0.005
    allowed = True

    async def guard():
        if not allowed:
            raise RuntimeError("lost")

    await lifecycle.startup()
    await lifecycle.start_heartbeat(guard)
    try:
        for _ in range(100):
            if repo.heartbeat.await_count >= 3:
                break
            await asyncio.sleep(0.005)
        assert repo.heartbeat.await_count >= 3
        assert recovery.await_count == 0
        allowed = False
        await asyncio.sleep(0.02)
        count = repo.heartbeat.await_count
        await asyncio.sleep(0.02)
        assert repo.heartbeat.await_count == count
    finally:
        await lifecycle.aclose()
    await asyncio.sleep(0.01)
    assert repo.heartbeat.await_count == count
