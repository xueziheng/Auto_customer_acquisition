"""notification worker 的最小 live/ready health server。"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from socket import socket

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.errors import ValidationError

_CHECKPOINTS = frozenset({"config", "schema", "database", "registry"})


class _NoSignalUvicornServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self._listening = asyncio.Event()

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield

    async def startup(self, sockets: list[socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            self._listening.set()

    async def wait_started(self) -> None:
        await self._listening.wait()


@dataclass
class NotificationHealthState:
    _ready: set[str] = field(default_factory=set, repr=False)
    _degraded: bool = field(default=False, repr=False)

    @property
    def is_ready(self) -> bool:
        return self._ready == _CHECKPOINTS and not self._degraded

    def mark_ready(self, checkpoint: str) -> None:
        if checkpoint not in _CHECKPOINTS:
            raise ValidationError("health checkpoint 无效")
        self._ready.add(checkpoint)

    def mark_degraded(self) -> None:
        self._degraded = True

    def mark_ok(self) -> None:
        self._degraded = False


def create_notification_health_app(state: NotificationHealthState) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.router.redirect_slashes = False

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        return JSONResponse(
            {"status": "ready" if state.is_ready else "not_ready"},
            status_code=200 if state.is_ready else 503,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        _request: object, error: StarletteHTTPException
    ) -> JSONResponse:
        if error.status_code == 405:
            return JSONResponse(
                {"code": "method_not_allowed", "message": "方法不允许"},
                status_code=405,
            )
        return JSONResponse(
            {"code": "not_found", "message": "资源不存在"}, status_code=404
        )

    return app


class NotificationHealthServer:
    def __init__(self, state: NotificationHealthState, port: int) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValidationError("health port 无效")
        self.config = uvicorn.Config(
            create_notification_health_app(state),
            host="0.0.0.0",
            port=port,
            access_log=False,
            log_config=None,
        )
        self._server = _NoSignalUvicornServer(self.config)

    async def serve(self) -> None:
        await self._server.serve()

    async def wait_started(self) -> None:
        await self._server.wait_started()

    async def close(self) -> None:
        self._server.should_exit = True
