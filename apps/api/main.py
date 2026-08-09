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

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from .dependencies import (
    ApiDependencies,
    UnconfiguredApiDependencies,
)
from .middleware import (
    ApiErrorResponse,
    ApiSettings,
    SafeUnhandledExceptionMiddleware,
    TenantAssertionMiddleware,
    install_error_handlers,
)
from .routers.crm import OpportunityIntakeBody
from .routers.crm import router as crm_router

_UNCONFIGURED_TENANT = "__tradeos_unconfigured__"
_DEFAULT_RETRY_AFTER_SECONDS = 30


def _install_openapi_contract(app: FastAPI) -> None:
    """把运行时统一 validation 400 显式写入并移除未实现的默认 422。"""
    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        intake_schema = OpportunityIntakeBody.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        definitions = intake_schema.pop("$defs", {})
        components.update(definitions)
        components["OpportunityIntakeBody"] = intake_schema
        components["ApiErrorResponse"] = ApiErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        validation_response = {
            "description": "请求参数无效",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ApiErrorResponse"}
                }
            },
        }
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                responses = operation["responses"]
                responses.pop("422", None)
                responses["400"] = validation_response
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


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
    _install_openapi_contract(app)
    return app


def main() -> None:
    """以 factory 模式启动；未注入 composition 时所有业务依赖失败关闭。"""
    import uvicorn

    uvicorn.run("apps.api.main:create_app", factory=True, access_log=False)


if __name__ == "__main__":
    main()
