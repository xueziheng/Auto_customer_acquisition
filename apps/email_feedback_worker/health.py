"""邮件反馈 worker 的最小 live/ready health server。"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.errors import ValidationError

_CHECKPOINTS = frozenset({"config", "schema", "database", "registry"})


class _NoSignalUvicornServer(uvicorn.Server):
    """进程信号只由 worker stop flag 处理，health server 不抢占。"""

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@dataclass
class EmailFeedbackHealthState:
    """只保存低基数 readiness 与 provider 状态。"""

    _ready: set[str] = field(default_factory=set, repr=False)
    _provider_degraded: bool = field(default=False, repr=False)
    disabled: bool = False

    @property
    def is_ready(self) -> bool:
        return not self.disabled and self._ready == _CHECKPOINTS

    @property
    def provider(self) -> str:
        return "degraded" if self._provider_degraded else "ok"

    def mark_ready(self, checkpoint: str) -> None:
        if checkpoint not in _CHECKPOINTS:
            raise ValidationError("health checkpoint 无效")
        self._ready.add(checkpoint)

    def mark_provider_degraded(self) -> None:
        self._provider_degraded = True

    def mark_provider_ok(self) -> None:
        self._provider_degraded = False


def create_health_app(state: EmailFeedbackHealthState) -> FastAPI:
    """只暴露两个固定路径；错误 body 也保持固定。"""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.router.redirect_slashes = False

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        payload = {
            "status": "ready" if state.is_ready else "not_ready",
            "provider": state.provider,
        }
        return JSONResponse(payload, status_code=200 if state.is_ready else 503)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: object, error: StarletteHTTPException) -> JSONResponse:
        if error.status_code == 405:
            return JSONResponse(
                {"code": "method_not_allowed", "message": "方法不允许"},
                status_code=405,
            )
        return JSONResponse(
            {"code": "not_found", "message": "资源不存在"}, status_code=404
        )

    return app


class EmailFeedbackHealthServer:
    """固定 bind 与关闭语义的 uvicorn 包装器。"""

    def __init__(self, state: EmailFeedbackHealthState, port: int) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValidationError("health port 无效")
        self.config = uvicorn.Config(
            create_health_app(state),
            host="0.0.0.0",
            port=port,
            access_log=False,
            log_config=None,
        )
        self._server = _NoSignalUvicornServer(self.config)

    async def serve(self) -> None:
        await self._server.serve()

    async def close(self) -> None:
        self._server.should_exit = True
