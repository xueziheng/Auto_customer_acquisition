"""同源真实会话边界；认证材料仅在传输与认证服务之间流动。"""

from __future__ import annotations

import re
from datetime import datetime
from http.cookies import CookieError, SimpleCookie

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette._utils import get_route_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domains.employees.schemas import EmployeeView
from shared.authentication import (
    AuthenticationDenied,
    AuthenticationInputInvalid,
    AuthenticationService,
    AuthPrincipal,
    normalize_login_username,
)

from .middleware import AnonymousRouteMatcher, ApiSettings, _error_response

SESSION_COOKIE_PREFIX = "tradeos_session_"
CSRF_HEADER = "X-CSRF-Token"
REQUEST_HEADER = "X-TradeOS-Request"
COOKIE_PATH = "/api"


class LoginRequest(BaseModel):
    """仅用于登录的小型输入；原值不得进入错误诊断。"""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=254)
    password: SecretStr = Field(repr=False)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        try:
            return normalize_login_username(value)
        except AuthenticationInputInvalid:
            raise ValueError("登录用户名无效") from None


class SessionResponse(BaseModel):
    """私有 no-store 会话视图；禁止将此响应记录到日志。"""

    model_config = ConfigDict(frozen=True)
    employee: EmployeeView
    csrf_token: str = Field(repr=False)
    expires_at: datetime


def validate_authentication_configuration(
    settings: ApiSettings,
    authentication: AuthenticationService | None,
    origin: str | None,
) -> None:
    """真实会话只允许精确 IPv4 loopback HTTP origin，与 dev 模式互斥。"""
    if authentication is None:
        if origin is not None:
            raise ValueError("authentication_configuration_invalid")
        return
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", origin or "")
    if settings.dev_mode or match is None or not 1 <= int(match[1]) <= 65535:
        raise ValueError("authentication_configuration_invalid")


def header_values(scope: Scope, name: bytes) -> list[str]:
    """读取原始头，绝不把重复安全头合并为单一断言。"""
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", ())
        if key.lower() == name
    ]


def session_cookie_name(origin: str) -> str:
    """Cookie 无端口隔离；按已校验 origin 的端口隔离同机 profile。"""
    return SESSION_COOKIE_PREFIX + origin.rsplit(":", 1)[1]


def session_token(scope: Scope, cookie_name: str) -> SecretStr | None:
    """拒绝重复 cookie 行及重名 cookie，避免各层解析差异。"""
    values = header_values(scope, b"cookie")
    if not values:
        return None
    if len(values) != 1:
        raise AuthenticationDenied()
    pairs = [piece.strip().partition("=") for piece in values[0].split(";")]
    names = [name for name, separator, _ in pairs if separator]
    if len(set(names)) != len(names):
        raise AuthenticationDenied()
    cookie = SimpleCookie()
    try:
        cookie.load(values[0])
    except CookieError:
        raise AuthenticationDenied() from None
    item = cookie.get(cookie_name)
    return SecretStr(item.value) if item is not None else None


class SessionAuthenticationMiddleware:
    """先校验来源与会话，再设置可信 Principal；业务写入沿用原 router 协议。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: ApiSettings,
        authentication: AuthenticationService,
        origin: str,
        anonymous_route_matcher: AnonymousRouteMatcher,
    ) -> None:
        self._app = app
        self._settings = settings
        self._authentication = authentication
        self._origin = origin
        self._cookie_name = session_cookie_name(origin)
        self._matcher = anonymous_route_matcher

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        original_send = send

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() != b"cache-control"
                    ]
                    + [(b"cache-control", b"no-store")],
                }
            await original_send(message)

        send = private_send
        path = get_route_path(scope)
        method = scope["method"]

        async def deny(status: int, code: str) -> None:
            await _error_response(
                status,
                code=code,
                message="请求身份或来源无效",
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)

        if header_values(scope, b"host") != [self._origin.removeprefix("http://")]:
            await deny(403, "authentication_origin_rejected")
            return
        if header_values(scope, b"x-employee-id") or header_values(
            scope, b"x-tenant-id"
        ):
            await deny(403, "development_identity_rejected")
            return
        # 原 capability 退订独立协议，不增加匿名路径，也不强加浏览器 CSRF。
        if self._matcher(method, path):
            await self._app(scope, receive, send)
            return
        security_names = (
            b"origin",
            b"x-tradeos-request",
            b"x-csrf-token",
            b"sec-fetch-site",
            b"content-type",
        )
        if any(len(header_values(scope, name)) > 1 for name in security_names):
            await deny(403, "authentication_headers_rejected")
            return
        origin = header_values(scope, b"origin")
        fetch = header_values(scope, b"sec-fetch-site")
        if (origin and origin != [self._origin]) or (
            fetch and fetch != ["same-origin"]
        ):
            await deny(403, "authentication_origin_rejected")
            return
        unsafe = method not in {"GET", "HEAD", "OPTIONS"}
        if unsafe and (
            origin != [self._origin]
            or header_values(scope, b"x-tradeos-request") != ["1"]
        ):
            await deny(403, "authentication_origin_rejected")
            return
        try:
            token = session_token(scope, self._cookie_name)
        except AuthenticationDenied:
            await deny(401, "authentication_required")
            return
        state = scope.setdefault("state", {})
        state["session_token"] = token
        if (method == "POST" and path == "/auth/login") or (
            method == "GET" and path in {"/health/live", "/health/ready"}
        ):
            await self._app(scope, receive, send)
            return
        csrf = header_values(scope, b"x-csrf-token")
        if token is None or (unsafe and (len(csrf) != 1 or not csrf[0])):
            await deny(401 if token is None else 403, "authentication_required")
            return
        try:
            principal = await self._authentication.authenticate(
                token, csrf_token=SecretStr(csrf[0]) if unsafe else None
            )
            if (
                not isinstance(principal, AuthPrincipal)
                or principal.tenant_id != self._settings.tenant
                or not principal.employee_id
                or not principal.user_id
            ):
                raise AuthenticationDenied()
        except AuthenticationDenied:
            await deny(401, "authentication_required")
            return
        state["auth_principal"] = principal
        state["tenant_id"] = principal.tenant_id
        await self._app(scope, receive, send)
