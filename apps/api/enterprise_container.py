"""受信注册的固定企业 ASGI 容器；不提供企业开户或动态租户装配。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shared.schemas.identifiers import TenantId

from .authentication import (
    AuthenticationCookieSettings,
    SessionAuthenticationMiddleware,
)
from .dependencies import ConfiguredApiDependencies
from .middleware import ApiSettings, _error_response

IsolationProbe = Callable[[AsyncEngine, str], Awaitable[None]]


class EnterpriseContainerConfigurationError(ValueError):
    """固定脱敏的受信企业注册配置错误。"""

    def __init__(self) -> None:
        super().__init__("enterprise_container_configuration_invalid")


class EnterpriseContainerStartupError(RuntimeError):
    """任一租户数据库或资源未就绪时整体失败关闭。"""

    def __init__(self) -> None:
        super().__init__("enterprise_container_startup_failed")


@dataclass(frozen=True)
class EnterpriseAppBinding:
    """服务器提供的固定企业实例；key 仅用于路由，不授予身份。"""

    key: str
    tenant_id: TenantId
    app: FastAPI

    @property
    def path(self) -> str:
        """返回与会话 cookie 精确一致的企业路由前缀。"""
        return "/api/enterprises/" + self.key


def _validate_bindings(
    bindings: Sequence[EnterpriseAppBinding],
) -> tuple[EnterpriseAppBinding, ...]:
    resolved = tuple(bindings)
    if not resolved:
        raise EnterpriseContainerConfigurationError()
    unique: dict[str, set[object]] = {
        key: set()
        for key in ("key", "tenant", "app", "auth", "engine", "dependencies", "cookie")
    }
    for binding in resolved:
        if (
            not isinstance(binding, EnterpriseAppBinding)
            or not isinstance(binding.key, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", binding.key) is None
            or not isinstance(binding.tenant_id, str)
            or not binding.tenant_id
            or binding.tenant_id != binding.tenant_id.strip()
            or not isinstance(binding.app, FastAPI)
        ):
            raise EnterpriseContainerConfigurationError()
        state = binding.app.state
        settings = getattr(state, "settings", None)
        authentication = getattr(state, "authentication", None)
        cookie = getattr(state, "authentication_cookie", None)
        dependencies = getattr(state, "dependencies", None)
        engine = getattr(state, "runtime_engine", None)
        middleware = [
            entry
            for entry in binding.app.user_middleware
            if entry.cls is SessionAuthenticationMiddleware
        ]
        if (
            not isinstance(settings, ApiSettings)
            or settings.dev_mode
            or settings.tenant != binding.tenant_id
            or authentication is None
            or not all(
                callable(getattr(authentication, method, None))
                for method in ("login", "authenticate", "get_session", "logout")
            )
            or not isinstance(cookie, AuthenticationCookieSettings)
            or cookie.path != binding.path
            or getattr(state, "authentication_cookie_name", None) != cookie.name
            or not isinstance(dependencies, ConfiguredApiDependencies)
            or not isinstance(engine, AsyncEngine)
            or len(middleware) != 1
        ):
            raise EnterpriseContainerConfigurationError()
        arguments = middleware[0].kwargs
        if (
            arguments.get("authentication") is not authentication
            or arguments.get("settings") is not settings
            or arguments.get("cookie") != cookie
            or arguments.get("origin") != getattr(state, "authentication_origin", None)
        ):
            raise EnterpriseContainerConfigurationError()
        values = {
            "key": binding.key,
            "tenant": binding.tenant_id,
            "app": id(binding.app),
            "auth": id(authentication),
            "engine": id(engine),
            "dependencies": id(dependencies),
            "cookie": cookie.name,
        }
        for name, value in values.items():
            if value in unique[name]:
                raise EnterpriseContainerConfigurationError()
            unique[name].add(value)
    return resolved


async def _database_isolation_probe(engine: AsyncEngine, tenant_id: str) -> None:
    from infra.db.tenant_security import assert_tenant_database_isolation

    await assert_tenant_database_isolation(engine, tenant_id)


class _ReadyGate:
    """lifespan 未完成或退出后不允许测试 transport 绕过真实启动门禁。"""

    def __init__(self, app: ASGIApp, *, is_ready: Callable[[], bool]) -> None:
        self._app, self._is_ready = app, is_ready

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._is_ready():
            await _error_response(
                503, code="enterprise_container_unavailable", message="企业服务尚未就绪"
            )(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() != b"cache-control"
                    ]
                    + [(b"cache-control", b"no-store")],
                }
            await send(message)

        await self._app(
            scope, receive, private_send if scope["type"] == "http" else send
        )


def create_enterprise_container(
    bindings: Sequence[EnterpriseAppBinding],
    *,
    isolation_probe: IsolationProbe | None = None,
) -> FastAPI:
    """挂载服务器固定实例，默认先验证真实数据库 RLS，再启动全部子资源。

    探针端口仅供可信装配/单测显式注入；HTTP 永远不能选择或禁用探针。
    容器仅提供 API，没有企业开户、身份 UI 或后台 worker 启用承诺。
    """
    registered = _validate_bindings(bindings)
    probe = (
        isolation_probe if isolation_probe is not None else _database_isolation_probe
    )
    if not callable(probe):
        raise EnterpriseContainerConfigurationError()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.enterprise_ready = False
        try:
            async with AsyncExitStack() as stack:
                _validate_bindings(registered)
                # 探针本身会打开连接；即便后续企业失败，也必须归还所有连接池。
                for binding in registered:
                    stack.push_async_callback(binding.app.state.runtime_engine.dispose)
                for binding in registered:
                    await probe(
                        binding.app.state.runtime_engine, str(binding.tenant_id)
                    )
                for binding in registered:
                    await stack.enter_async_context(
                        binding.app.router.lifespan_context(binding.app)
                    )
                app.state.enterprise_ready = True
                try:
                    yield
                finally:
                    app.state.enterprise_ready = False
        except Exception:  # noqa: BLE001 - 启动边界不传播数据库或资源异常原文
            raise EnterpriseContainerStartupError() from None
        finally:
            app.state.enterprise_ready = False

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.enterprise_ready = False
    app.state.enterprise_bindings = registered
    app.add_middleware(_ReadyGate, is_ready=lambda: app.state.enterprise_ready)
    for binding in registered:
        app.mount(binding.path, binding.app)
    return app
