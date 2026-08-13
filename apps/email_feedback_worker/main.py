"""Gmail 邮件反馈 worker 的生产进程入口。"""

from __future__ import annotations

import asyncio
import logging
import os

from infra.db.schema import DatabaseSchemaError

from .config import WorkerConfigurationError
from .runtime import (
    EmailFeedbackRuntimeFactory,
    RuntimeFactory,
    WorkerRunStatus,
    run_email_feedback_application,
)

logger = logging.getLogger("apps.email_feedback_worker")
_EXIT_CONFIGURATION = 2
_EXIT_STARTUP = 3
_EXIT_LOCK_NOT_ACQUIRED = 4
_EXIT_LOCK_LOST = 5


async def run_from_factory(
    runtime_factory: RuntimeFactory,
    *,
    stop_event: asyncio.Event | None = None,
) -> int:
    """在 factory 资源上下文内运行，并映射 typed lock 状态。"""
    async with runtime_factory() as application:
        result = await run_email_feedback_application(
            application, stop_event=stop_event
        )
    if result.status is WorkerRunStatus.LOCK_NOT_ACQUIRED:
        return _EXIT_LOCK_NOT_ACQUIRED
    if result.status is WorkerRunStatus.LOCK_LOST:
        return _EXIT_LOCK_LOST
    return 0


def main(runtime_factory: RuntimeFactory | None = None) -> int:
    """零参数读取生产环境；所有失败只输出固定中文分类。"""
    factory = (
        runtime_factory
        if runtime_factory is not None
        else EmailFeedbackRuntimeFactory(os.environ)
    )
    try:
        return asyncio.run(run_from_factory(factory))
    except WorkerConfigurationError as error:
        logger.error(
            "邮件反馈 worker 配置无效",
            extra={"config_name": error.field_name},
        )
        return _EXIT_CONFIGURATION
    except DatabaseSchemaError:
        logger.error("邮件反馈 worker schema 检查失败")
        return _EXIT_STARTUP
    except Exception as error:  # noqa: BLE001 - 进程边界不得泄漏异常内容
        logger.error(
            "邮件反馈 worker 启动失败",
            extra={"error_type": type(error).__name__},
        )
        return _EXIT_STARTUP


if __name__ == "__main__":
    raise SystemExit(main())
