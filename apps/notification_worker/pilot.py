"""本机持久站内通知入口，不承诺邮件送达。"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from infra.controlled.network import install_network_boundary
from infra.pilot.config import PilotConfig, PilotError
from shared.schemas.identifiers import TenantId

from .config import NotificationWorkerConfig
from .runtime import (
    NotificationRuntimeMode,
    notification_worker_runtime,
    run_notification_worker,
)


def runtime_settings(config: PilotConfig) -> NotificationWorkerConfig:
    """显式 LOCAL_IN_APP 只映射技术参数，绝不创建邮件客户端。"""
    env = config.runtime_environment()
    if env["TRADEOS_NOTIFICATION_DELIVERY_MODE"] != "LOCAL_IN_APP":
        raise PilotError("notification_mode_invalid")
    return NotificationWorkerConfig(
        config.database_url,
        TenantId(config.tenant_id),
        int(env["TRADEOS_NOTIFICATION_POLL_INTERVAL_SECONDS"]),
        int(env["TRADEOS_NOTIFICATION_BATCH_LIMIT"]),
        config.notification_port,
        env["TRADEOS_NOTIFICATION_LEASE_OWNER"],
    )


async def run(config: PilotConfig) -> int:
    """保留 canonical jobs/router 的实际持久投递和对称关闭。"""
    async with notification_worker_runtime(
        runtime_settings(config), mode=NotificationRuntimeMode.LOCAL_IN_APP
    ) as runtime:
        await run_notification_worker(runtime)
    return 0


def main() -> int:
    """只允许本 profile 的数据库与本站内健康端口。"""
    logging.disable(logging.CRITICAL)
    try:
        config = PilotConfig.read(Path(sys.argv[1]))
        install_network_boundary(
            destinations=frozenset({config.database_port}),
            listeners=frozenset({config.notification_port}),
        )
        return asyncio.run(run(config))
    except BaseException:  # noqa: BLE001 进程边界固定失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
