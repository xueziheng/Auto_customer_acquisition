"""模型调用期间阻止 SDK/HTTP 调试日志泄露输入、响应与凭证。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_private_call: ContextVar[bool] = ContextVar("deepseek_private_call", default=False)
_LOGGER_NAMES = (
    "openai._base_client", "openai._response", "httpx",
    "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)


class _PrivateCallFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_call.get()


@contextmanager
def private_sdk_logs() -> Iterator[None]:
    """按异步任务隔离；不修改全局日志级别，也不遮蔽其他任务的诊断。"""
    names = set(_LOGGER_NAMES)
    names.update(
        name for name in list(logging.Logger.manager.loggerDict)
        if name.startswith(("openai.", "httpcore.", "httpx."))
    )
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(item, _PrivateCallFilter) for item in logger.filters):
            logger.addFilter(_PrivateCallFilter())
    token = _private_call.set(True)
    try:
        yield
    finally:
        _private_call.reset(token)
