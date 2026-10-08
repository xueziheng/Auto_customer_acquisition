"""独立平台 API：仅认证和受审计概览，不挂载任何企业业务 router。"""
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domains.organization.platform_access import PlatformAccessService, PlatformOverview
from infra.authentication.service import PostgresAuthentication
from infra.db.platform_access import (
    EnterpriseReaderBinding,
    PostgresEnterpriseOverviewReader,
    PostgresPlatformAccess,
)
from infra.db.schema import assert_database_schema_current
from infra.db.tenant_security import assert_tenant_database_isolation
from shared.authentication import (
    AuthenticationDenied,
    AuthPrincipal,
    IssuedSession,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId

from .authentication import (
    AuthenticationCookieSettings,
    LoginRequest,
    SessionAuthenticationMiddleware,
    header_values,
    validate_authentication_configuration,
)
from .middleware import ApiSettings, _error_response
from .routers.authentication import install_authentication_errors


class PlatformSessionResponse(BaseModel):
    """平台会话无企业员工 DTO，不可作为企业工作台身份使用。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    username: str
    display_name: str
    role: Literal["platform_admin"] = "platform_admin"
    csrf_token: str = Field(repr=False)
    expires_at: datetime


class _PlatformAuthentication:
    """复用认证协议，同时在交付或接纳每个会话前重新读取平台授权。"""

    def __init__(
        self, authentication: PostgresAuthentication, access: PlatformAccessService,
    ) -> None:
        self._authentication = authentication
        self._access = access

    async def _authorize(self, principal: AuthPrincipal) -> None:
        try:
            await self._access.authorize(principal)
        except PermissionDenied:
            raise AuthenticationDenied() from None

    async def login(self, username: str, password: SecretStr) -> IssuedSession:
        issued = await self._authentication.login(username, password)
        try:
            await self._authorize(issued.principal)
        except Exception:
            await self._authentication.logout(issued.token)
            raise
        return issued

    async def authenticate(
        self, token: SecretStr, *, csrf_token: SecretStr | None = None,
    ) -> AuthPrincipal:
        principal = await self._authentication.authenticate(token, csrf_token=csrf_token)
        await self._authorize(principal)
        return principal

    async def get_session(self, token: SecretStr) -> IssuedSession:
        issued = await self._authentication.get_session(token)
        await self._authorize(issued.principal)
        return issued

    async def logout(self, token: SecretStr) -> None:
        await self._authentication.logout(token)


class _PlatformBoundary:
    """启动未验收时拒绝请求，并固定脱敏底层错误；不记录请求或认证材料。"""

    def __init__(self, app: ASGIApp, *, state: object) -> None:
        self._app, self._state = app, state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if not getattr(self._state, "platform_ready", False):
            await _error_response(
                503, code="platform_unavailable", message="平台服务尚未就绪",
            )(scope, receive, send)
            return
        started = False

        async def private_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {
                    **message,
                    "headers": [
                        (k, v) for k, v in message.get("headers", [])
                        if k.lower() != b"cache-control"
                    ] + [(b"cache-control", b"no-store")],
                }
            await send(message)

        try:
            await self._app(scope, receive, private_send)
        except Exception:  # noqa: BLE001 -- SQL 异常可能携带输入，禁止传播原始错误
            if started:
                raise RuntimeError("platform_response_failed") from None
            await _error_response(
                503, code="platform_unavailable", message="平台服务暂不可用",
            )(scope, receive, send)


def create_platform_app(
    *, control_tenant: TenantId, engine: AsyncEngine, origin: str,
    readers: tuple[EnterpriseReaderBinding, ...],
) -> FastAPI:
    """受信配置独立控制租户及固定业务读取器；本应用持有并释放控制引擎。"""
    settings = ApiSettings(
        tenant_id=control_tenant, dev_mode=False, retry_after_seconds=30,
    )
    ids = [binding.tenant_id for binding in readers]
    engines = [id(binding.engine) for binding in readers]
    if (
        len(ids) != len(set(ids)) or control_tenant in ids
        or len(engines) != len(set(engines)) or id(engine) in engines
        or any(not tenant or tenant != tenant.strip() for tenant in ids)
    ):
        raise ValueError("platform_configuration_invalid")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    access = PlatformAccessService(
        control_tenant, PostgresPlatformAccess(factory, control_tenant),
        {
            binding.tenant_id: PostgresEnterpriseOverviewReader(binding)
            for binding in readers
        },
    )
    authentication = _PlatformAuthentication(PostgresAuthentication(factory, control_tenant), access)
    validate_authentication_configuration(settings, authentication, origin)
    cookie = AuthenticationCookieSettings("tradeos_session_platform", "/api/platform")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.platform_ready = False
        try:
            try:
                await assert_database_schema_current(engine)
                await assert_tenant_database_isolation(engine, control_tenant)
                for binding in readers:
                    await assert_tenant_database_isolation(binding.engine, binding.tenant_id)
            except Exception:  # noqa: BLE001 -- 启动错误不得携带数据库连接材料
                raise RuntimeError("platform_database_isolation_failed") from None
            app.state.platform_ready = True
            yield
        finally:
            app.state.platform_ready = False
            primary = sys.exception()
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001 -- 清理不暴露驱动异常，也不覆盖原始失败/取消
                if primary is None:
                    raise RuntimeError("platform_database_close_failed") from None

    app = FastAPI(
        title="TradeOS 平台管理", docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan,
    )
    app.state.platform_ready = False
    app.state.settings = settings
    app.state.authentication = authentication
    app.state.authentication_cookie = cookie
    app.state.authentication_cookie_name = cookie.name
    app.state.authentication_origin = origin
    app.state.runtime_engine = engine
    app.state.platform_access = access
    install_authentication_errors(app)

    async def permission_denied(request: Request, error: Exception) -> Response:
        del request, error
        return _error_response(403, code="platform_access_denied", message="平台权限不足")

    app.add_exception_handler(PermissionDenied, permission_denied)

    async def session_response(issued: IssuedSession) -> PlatformSessionResponse:
        identity = await access.authorize(issued.principal)
        return PlatformSessionResponse(
            **identity.model_dump(), csrf_token=issued.csrf_token.get_secret_value(),
            expires_at=issued.expires_at,
        )

    @app.post(
        "/auth/login", response_model=PlatformSessionResponse,
        openapi_extra={"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": LoginRequest.model_json_schema()}},
        }},
    )
    async def login(request: Request, response: Response) -> PlatformSessionResponse | Response:
        if header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(415, code="authentication_json_required", message="登录需要JSON请求")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 4096 - len(body):
                return _error_response(413, code="authentication_body_too_large", message="登录请求过大")
            body.extend(chunk)
        try:
            credentials = LoginRequest.model_validate_json(bytes(body))
        except ValidationError:
            return _error_response(400, code="authentication_input_invalid", message="登录资料无效")
        issued = await authentication.login(credentials.username, credentials.password)
        try:
            result = await session_response(issued)
            old = getattr(request.state, "session_token", None)
            if isinstance(old, SecretStr):
                await authentication.logout(old)
        except Exception:
            await authentication.logout(issued.token)
            raise
        response.set_cookie(
            cookie.name, issued.token.get_secret_value(), path=cookie.path,
            httponly=True, samesite="strict", expires=issued.expires_at,
        )
        return result

    @app.get("/auth/session", response_model=PlatformSessionResponse)
    async def current_session(request: Request) -> PlatformSessionResponse:
        token = getattr(request.state, "session_token", None)
        if not isinstance(token, SecretStr):
            raise AuthenticationDenied()
        return await session_response(await authentication.get_session(token))

    @app.post("/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        token = getattr(request.state, "session_token", None)
        if not isinstance(token, SecretStr):
            raise AuthenticationDenied()
        await authentication.logout(token)
        response = Response(status_code=204)
        response.delete_cookie(cookie.name, path=cookie.path, httponly=True, samesite="strict")
        return response

    @app.get("/overview", response_model=PlatformOverview)
    async def overview(request: Request) -> PlatformOverview | Response:
        if request.query_params:
            return _error_response(400, code="platform_query_invalid", message="平台请求参数无效")
        principal = getattr(request.state, "auth_principal", None)
        if not isinstance(principal, AuthPrincipal):
            raise AuthenticationDenied()
        return await access.overview(principal)

    app.add_middleware(
        SessionAuthenticationMiddleware, settings=settings,
        authentication=authentication, origin=origin,
        anonymous_route_matcher=lambda _method, _path: False, cookie=cookie,
    )
    app.add_middleware(_PlatformBoundary, state=app.state)
    return app
