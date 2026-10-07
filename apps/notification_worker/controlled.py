"""本owner专用站内通知入口；不配置邮件、不共享其它apps进程对象。"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from infra.controlled.config import ControlledConfig
from infra.controlled.network import install_network_boundary
from shared.schemas.identifiers import TenantId

from .config import NotificationWorkerConfig
from .runtime import (
    NotificationRuntimeMode,
    notification_worker_runtime,
    run_notification_worker,
)


async def _run(config: ControlledConfig) -> int:
    settings = NotificationWorkerConfig(
        config.database_url,
        TenantId(config.tenant_id),
        1,
        20,
        config.notification_port,
        "controlled-notification",
    )
    async with notification_worker_runtime(
        settings, mode=NotificationRuntimeMode.CONTROLLED_IN_APP
    ) as runtime:
        await run_notification_worker(runtime)
    return 0


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        config = ControlledConfig.read(Path(sys.argv[1]))
        install_network_boundary(
            destinations=frozenset({config.database_port}),
            listeners=frozenset({config.notification_port}),
        )
        return asyncio.run(_run(config))
    except BaseException:  # noqa: BLE001 固定进程退出，不输出底层异常或凭证
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
