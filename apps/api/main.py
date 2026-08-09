"""API 进程入口。

职责（实现时）：
1. FastAPI 实例与生命周期
2. 依赖注入装配：Repository 实现、EventBus、ToolRegistry、
   ConnectorRegistry、各域服务实例
3. 中间件：租户上下文（从认证解析 tenant_id 并贯穿请求）、
   审计、错误转换（域错误 → 结构化 HTTP 响应，
   is_retryable → Retry-After 头）
4. 挂载 routers/ 下全部 router

启动前置校验（失败即拒绝启动，不要带病上线）：
- Playbook 已配置（organization 域）
- ConnectorRegistry 密钥齐备
- 数据库迁移版本匹配
"""

from __future__ import annotations

from fastapi import FastAPI

from .dependencies import (
    ApiDependencies,
    UnconfiguredApiDependencies,
)
from .middleware import (
    ApiSettings,
    SafeUnhandledExceptionMiddleware,
    TenantAssertionMiddleware,
    install_error_handlers,
)
from .routers.crm import router as crm_router

_UNCONFIGURED_TENANT = "__tradeos_unconfigured__"
_DEFAULT_RETRY_AFTER_SECONDS = 30


def create_app(
    *,
    settings: ApiSettings | None = None,
    dependencies: ApiDependencies | None = None,
) -> FastAPI:
    """构造互相隔离的 API app。

    zero-arg 只产生固定未配置、失败关闭的 app，供 uvicorn factory 与确定性
    OpenAPI 导出使用；不读取环境、不创建数据库资源、不注册空 handler。
    """
    resolved_settings = settings or ApiSettings(
        tenant_id=_UNCONFIGURED_TENANT,
        dev_mode=False,
        retry_after_seconds=_DEFAULT_RETRY_AFTER_SECONDS,
    )
    resolved_dependencies = dependencies or UnconfiguredApiDependencies()

    app = FastAPI(title="TradeOS API", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.dependencies = resolved_dependencies
    install_error_handlers(app, resolved_settings)
    app.add_middleware(TenantAssertionMiddleware, settings=resolved_settings)
    # Starlette 后加的 user middleware 位于外层：安全边界必须包住其余 user middleware。
    app.add_middleware(SafeUnhandledExceptionMiddleware)
    app.include_router(crm_router, prefix="/crm")
    return app


def main() -> None:
    """以 factory 模式启动；未注入 composition 时所有业务依赖失败关闭。"""
    import uvicorn

    uvicorn.run("apps.api.main:create_app", factory=True)


if __name__ == "__main__":
    main()
