"""本owner真实PG job到站内持久送达，受控渠道能力明确。"""

import asyncio
import importlib
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from notification_gateway.jobs import (
    NotificationContext,
    NotificationJob,
    NotificationKind,
)
from notification_gateway.models import NotificationPriority
from scripts.run_web_core_controlled import reserve
from shared.schemas.identifiers import EmployeeId, NotificationJobId, TenantId, new_id
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure as _owned_infrastructure,
)

owned_infrastructure = _owned_infrastructure


async def test_controlled_job_is_actually_delivered_with_priority_and_dedup(
    owned_infrastructure,
):
    module = importlib.import_module("apps.notification_worker.runtime")
    mode = getattr(module, "NotificationRuntimeMode", None)
    assert mode is not None, "缺少显式受控站内装配"
    from apps.notification_worker.config import NotificationWorkerConfig

    config = owned_infrastructure.config
    tenant = TenantId(config.tenant_id)
    listener = reserve(0)
    port = listener.getsockname()[1]
    listener.close()
    settings = NotificationWorkerConfig(
        config.database_url, tenant, 1, 10, port, "controlled-notification"
    )
    engine = create_engine_from(config.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for index in range(2):
            async with module.notification_worker_runtime(
                settings, mode=mode.CONTROLLED_IN_APP
            ) as runtime:
                if index == 0:
                    for priority in (NotificationPriority.URGENT,):
                        job = NotificationJob(
                            NotificationJobId(new_id("njb")),
                            tenant,
                            EmployeeId(new_id("emp")),
                            priority,
                            NotificationContext(
                                NotificationKind.COMMITMENT_OVERDUE,
                                new_id("com"),
                                None,
                                None,
                                None,
                            ),
                            "f" * 64,
                            "CommitmentOverdue",
                            new_id("dedup"),
                            datetime.now(UTC),
                        )
                        assert await runtime.jobs.enqueue(job)
                stop = asyncio.Event()

                async def wait(seconds, event):
                    event.set()

                result = await module.run_notification_worker(
                    runtime, stop_event=stop, wait=wait
                )
                assert result.jobs_completed == (1 if index == 0 else 0)
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT priority FROM in_app_notifications WHERE tenant_id=:t ORDER BY priority"
                        ),
                        {"t": tenant},
                    )
                )
                .scalars()
                .all()
            )
            assert rows == ["urgent"]
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM notification_jobs WHERE tenant_id=:t AND status='completed'"
                    ),
                    {"t": tenant},
                )
                == 1
            )
    finally:
        await engine.dispose()


async def test_cancel_after_socket_bind_before_runtime_ready_reclaims_listener(
    owned_infrastructure, monkeypatch,
):
    """监听已存在但runtime尚未ready的取消窗口，不泄漏socket或lifespan任务。"""
    import pytest

    from apps.notification_worker import health
    from apps.notification_worker.config import NotificationWorkerConfig
    from apps.notification_worker.runtime import (
        NotificationRuntimeMode,
        notification_worker_runtime,
    )

    config = owned_infrastructure.config
    listener = reserve(0)
    port = listener.getsockname()[1]
    listener.close()
    bound = asyncio.Event()
    original_wait_started = health.NotificationHealthServer.wait_started
    servers = []

    async def hold_ready(server):
        servers.append(server)
        await original_wait_started(server)
        bound.set()
        await asyncio.Future()

    monkeypatch.setattr(health.NotificationHealthServer, "wait_started", hold_ready)
    settings = NotificationWorkerConfig(
        config.database_url, TenantId(config.tenant_id), 1, 10, port, "cancel-startup"
    )
    before = asyncio.all_tasks()

    async def consume():
        async with notification_worker_runtime(
            settings, mode=NotificationRuntimeMode.CONTROLLED_IN_APP
        ):
            pytest.fail("ready信号尚未返回")

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(bound.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        reopened = reserve(port)
        reopened.close()
        remaining = {t for t in asyncio.all_tasks() - before if not t.done()}
        assert remaining == set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # RED时也只回收本测试真实创建的server，避免遗留监听影响后续。
        for server in servers:
            if getattr(server._server, "servers", ()):
                await server._server.shutdown()
