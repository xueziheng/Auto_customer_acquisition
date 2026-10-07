"""notification worker 的生产进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os

from infra.db.schema import DatabaseSchemaError

from .config import (
    NotificationWorkerConfig,
    NotificationWorkerConfigurationError,
)
from .runtime import notification_worker_runtime, run_notification_worker

logger = logging.getLogger("apps.notification_worker")
_EXIT_CONFIGURATION = 2
_EXIT_STARTUP = 3


async def _run(config: NotificationWorkerConfig) -> int:
    async with notification_worker_runtime(config) as runtime:
        await run_notification_worker(runtime)
    return 0


def main() -> int:
    """只读显式环境；错误输出固定且不包含 DSN/异常消息。"""
    try:
        config = NotificationWorkerConfig.from_environ(os.environ)
        return asyncio.run(_run(config))
    except NotificationWorkerConfigurationError as error:
        logger.error(
            "通知 worker 配置无效",
            extra={"config_name": error.field_name},
        )
        return _EXIT_CONFIGURATION
    except DatabaseSchemaError:
        logger.error("通知 worker schema 检查失败")
        return _EXIT_STARTUP
    except Exception as error:  # noqa: BLE001
        logger.error(
            "通知 worker 启动失败",
            extra={"error_type": type(error).__name__},
        )
        return _EXIT_STARTUP


if __name__ == "__main__":
    raise SystemExit(main())
