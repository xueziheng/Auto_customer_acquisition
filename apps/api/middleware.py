"""API 边界：严格租户断言与脱敏错误响应。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    PolicyViolation,
    TradeOSError,
    ValidationError,
)
from shared.schemas.identifiers import TenantId

logger = logging.getLogger(__name__)


class _ResponseSendFailed(BaseException):
    """wire send 普通失败的私有控制流；无消息且绝不越过本中间件。"""


class ApiSettings(BaseModel):
    """API 可注入配置；不读取环境变量，不包含数据库连接信息。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: str
    dev_mode: bool
    retry_after_seconds: int = Field(gt=0)

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, value: str) -> str:
        """拒绝空白或带边界空白的租户值，绝不静默改写配置。"""
        if not value or not value.strip() or value != value.strip():
            raise ValueError("tenant_id 必须是非空且无边界空白的字符串")
        return value

    @property
    def tenant(self) -> TenantId:
        """返回固定租户的强类型视图。"""
        return TenantId(self.tenant_id)


class ApiErrorResponse(BaseModel):
    """所有 HTTP 错误共用的扁平外部契约。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    code: str
    message: str


def _error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造只含固定安全字段的错误响应。"""
    payload = ApiErrorResponse(code=code, message=message)
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


class AnonymousRouteMatcher(Protocol):
    def __call__(self, method: str, path: str) -> bool: ...


class TenantAssertionMiddleware:
    """对每个 HTTP 请求先做固定单租户精确断言。

    请求头值不做 strip、大小写转换或回显；重复头视为歧义并拒绝。
    WebSocket/lifespan 不属于本切片 HTTP 契约，原样交给下层。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: ApiSettings,
        anonymous_route_matcher: AnonymousRouteMatcher | None = None,
    ) -> None:
        self._app = app
        self._settings = settings
        self._anonymous_route_matcher = anonymous_route_matcher

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        matcher = self._anonymous_route_matcher
        if matcher is not None and matcher(scope["method"], scope["path"]):
            await self._app(scope, receive, send)
            return

        values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"x-tenant-id"
        ]
        if not values:
            response = _error_response(
                401,
                code="tenant_header_required",
                message="缺少租户身份",
            )
            await response(scope, receive, send)
            return
        if len(values) != 1 or values[0] != self._settings.tenant_id:
            response = _error_response(
                403,
                code="tenant_forbidden",
                message="租户身份不匹配",
            )
            await response(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["tenant_id"] = self._settings.tenant
        await self._app(scope, receive, send)


class SafeUnhandledExceptionMiddleware:
    """在 ServerErrorMiddleware 内侧吞掉 HTTP 未分类异常，避免原文被重抛记录。

    本中间件必须位于所有 user middleware 的最外层、``ExceptionMiddleware`` 外侧。
    已开始但未完成的响应只能用空 body 安全收尾；已完成的响应不再二次写入。
    ``CancelledError`` 不属于 ``Exception``，自然传播；非 HTTP scope 原样传播，
    因而不会隐藏 lifespan 启动故障。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        response_started = False
        response_completed = False
        def log_send_failure(error: Exception) -> None:
            logger.error(
                "API 响应发送失败",
                extra={"error_type": type(error).__name__},
            )

        async def safe_send(message: Message) -> None:
            outcome = (await asyncio.gather(send(message), return_exceptions=True))[0]
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome from None
                log_send_failure(outcome)
                raise _ResponseSendFailed from None

        async def tracking_send(message: Message) -> None:
            nonlocal response_started, response_completed
            await safe_send(message)
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_completed = True

        outcome = (
            await asyncio.gather(
                self._app(scope, receive, tracking_send),
                return_exceptions=True,
            )
        )[0]
        if isinstance(outcome, _ResponseSendFailed):
            return
        if not isinstance(outcome, BaseException):
            return
        if not isinstance(outcome, Exception):
            raise outcome from None
        logger.error(
            "API 处理请求时发生未分类异常",
            extra={"error_type": type(outcome).__name__},
        )
        if response_completed:
            return
        if response_started:
            completion = (
                await asyncio.gather(
                    safe_send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    ),
                    return_exceptions=True,
                )
            )[0]
            if isinstance(completion, _ResponseSendFailed):
                return
            if isinstance(completion, BaseException):
                raise completion from None
            return
        response = _error_response(
            500,
            code="internal_error",
            message="服务处理请求失败",
        )
        fallback = (
            await asyncio.gather(
                response(scope, receive, tracking_send),
                return_exceptions=True,
            )
        )[0]
        if isinstance(fallback, _ResponseSendFailed):
            return
        if isinstance(fallback, BaseException):
            raise fallback from None


ExceptionHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def install_error_handlers(app: FastAPI, settings: ApiSettings) -> None:
    """为单个 app 安装独立错误处理器，不共享可变 handler 状态。"""

    async def tradeos_error_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        del request
        tradeos_error = cast(TradeOSError, error)
        if isinstance(tradeos_error, ValidationError):
            return _error_response(
                400, code="validation_error", message="请求参数无效"
            )
        if isinstance(tradeos_error, PermissionDenied):
            return _error_response(403, code="forbidden", message="没有权限")
        if isinstance(tradeos_error, InvalidStateTransition):
            return _error_response(
                409,
                code="invalid_state",
                message="当前状态不允许此操作",
            )
        if tradeos_error.is_retryable:
            return _error_response(
                503,
                code="service_unavailable",
                message="服务暂时不可用",
                headers={"Retry-After": str(settings.retry_after_seconds)},
            )
        if isinstance(tradeos_error, PolicyViolation):
            return _error_response(
                400,
                code="request_rejected",
                message="请求被安全策略拒绝",
            )
        return _error_response(400, code="request_error", message="请求无法处理")

    async def request_validation_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        del request, error
        return _error_response(
            400, code="validation_error", message="请求参数无效"
        )

    async def http_error_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        del request
        http_error = cast(StarletteHTTPException, error)
        if http_error.status_code == 401:
            return _error_response(
                401,
                code="authentication_required",
                message="需要员工身份",
            )
        if http_error.status_code == 403:
            return _error_response(403, code="forbidden", message="没有权限")
        if http_error.status_code == 404:
            return _error_response(404, code="not_found", message="资源不存在")
        return _error_response(
            http_error.status_code,
            code="http_error",
            message="请求未完成",
        )

    app.add_exception_handler(
        TradeOSError, cast(ExceptionHandler, tradeos_error_handler)
    )
    app.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, request_validation_handler),
    )
    app.add_exception_handler(
        PydanticError,
        cast(ExceptionHandler, request_validation_handler),
    )
    app.add_exception_handler(
        StarletteHTTPException,
        cast(ExceptionHandler, http_error_handler),
    )
