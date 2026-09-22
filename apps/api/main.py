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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.types import Lifespan

from apps.api.routers.assistant import router as assistant_router
from shared.authentication import AuthenticationService
from shared.schemas.runtime_capabilities import CapabilityName, RuntimeCapability
from workflows.email_feedback.unsubscribe import UnsubscribeService

from .authentication import (
    LoginRequest,
    SessionAuthenticationMiddleware,
    session_cookie_name,
    validate_authentication_configuration,
)
from .dependencies import (
    ApiDependencies,
    ConfiguredApiDependencies,
    UnconfiguredApiDependencies,
)
from .middleware import (
    ApiErrorResponse,
    ApiSettings,
    SafeUnhandledExceptionMiddleware,
    TenantAssertionMiddleware,
    install_error_handlers,
)
from .routers.approvals import router as approvals_router
from .routers.authentication import install_authentication_errors
from .routers.authentication import router as authentication_router
from .routers.campaigns import router as campaigns_router
from .routers.command_center import router as command_center_router
from .routers.commitments import router as commitments_router
from .routers.costing_quotes import router as costing_quotes_router
from .routers.crm import OpportunityIntakeBody
from .routers.crm import router as crm_router
from .routers.customer_discovery import router as customer_discovery_router
from .routers.demand_radar import router as demand_radar_router
from .routers.email_inbound import router as email_inbound_router
from .routers.health import ReadinessProbe, build_capability_router, build_health_router
from .routers.inbox import router as inbox_router
from .routers.notifications import router as notifications_router
from .routers.products import router as products_router
from .routers.quotation_actions import router as quotation_actions_router
from .routers.runs import router as runs_router
from .routers.sending_identities import router as sending_identities_router
from .routers.settings import router as settings_router
from .routers.sourcing import router as sourcing_router
from .routers.team import router as team_router
from .routers.unsubscribe import (
    is_anonymous_unsubscribe_route,
)
from .routers.unsubscribe import (
    router as unsubscribe_router,
)
from .routers.work_uploads import router as work_uploads_router

_UNCONFIGURED_TENANT = "__tradeos_unconfigured__"
_DEFAULT_RETRY_AFTER_SECONDS = 30


def _install_openapi_contract(app: FastAPI) -> None:
    """写入统一 400，并只保留显式声明的安全 422 契约。"""

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
        components["LoginRequest"] = LoginRequest.model_json_schema()
        components["ApiErrorResponse"] = ApiErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        api_error_schema = {"$ref": "#/components/schemas/ApiErrorResponse"}
        validation_response = {
            "description": "请求参数无效",
            "content": {"application/json": {"schema": api_error_schema}},
        }
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                responses = operation["responses"]
                response_422 = responses.get("422")
                response_422_schema = (
                    response_422.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                    if isinstance(response_422, dict)
                    else None
                )
                if response_422_schema != api_error_schema:
                    responses.pop("422", None)
                responses["400"] = validation_response
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def create_app(
    *,
    settings: ApiSettings | None = None,
    dependencies: ApiDependencies | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
    cors_allowed_origins: tuple[str, ...] = (),
    readiness_probe: ReadinessProbe | None = None,
    unsubscribe_service: UnsubscribeService | None = None,
    authentication: AuthenticationService | None = None,
    authentication_origin: str | None = None,
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
    validate_authentication_configuration(
        resolved_settings, authentication, authentication_origin
    )
    resolved_dependencies = dependencies or UnconfiguredApiDependencies()
    resolved_unsubscribe_service = unsubscribe_service
    if resolved_unsubscribe_service is None and isinstance(
        resolved_dependencies, ConfiguredApiDependencies
    ):
        resolved_unsubscribe_service = resolved_dependencies.unsubscribe_service

    app = FastAPI(title="TradeOS API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.dependencies = resolved_dependencies
    app.state.unsubscribe_service = resolved_unsubscribe_service
    app.state.authentication = authentication
    app.state.authentication_cookie_name = (
        session_cookie_name(authentication_origin)
        if authentication_origin is not None
        else None
    )
    install_error_handlers(app, resolved_settings)
    install_authentication_errors(app)
    app.include_router(authentication_router)
    if authentication is not None:
        assert authentication_origin is not None
        app.add_middleware(
            SessionAuthenticationMiddleware,
            settings=resolved_settings,
            authentication=authentication,
            origin=authentication_origin,
            anonymous_route_matcher=is_anonymous_unsubscribe_route,
        )
    else:
        app.add_middleware(
            TenantAssertionMiddleware,
            settings=resolved_settings,
            anonymous_route_matcher=is_anonymous_unsubscribe_route,
        )
    if cors_allowed_origins and authentication is None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Idempotency-Key",
                "X-Employee-Id",
                "X-Tenant-Id",
            ],
        )
    # Starlette 后加的 user middleware 位于外层：安全边界必须包住其余 user middleware。
    app.add_middleware(SafeUnhandledExceptionMiddleware)
    app.include_router(crm_router, prefix="/crm")
    app.include_router(campaigns_router, prefix="/crm")
    app.include_router(sending_identities_router, prefix="/crm")
    app.include_router(customer_discovery_router, prefix="/prospects")
    app.include_router(demand_radar_router, prefix="/demand")
    app.include_router(command_center_router, prefix="/commands")
    app.include_router(assistant_router)
    app.include_router(approvals_router)
    app.include_router(notifications_router)
    app.include_router(inbox_router)
    app.include_router(email_inbound_router)
    app.include_router(products_router, prefix="/products")
    app.include_router(sourcing_router)
    app.include_router(costing_quotes_router, prefix="/costing-quotes")
    app.include_router(quotation_actions_router, prefix="/costing-quotes")
    app.include_router(team_router, prefix="/team")
    app.include_router(work_uploads_router, prefix="/work-uploads")
    app.include_router(commitments_router, prefix="/commitments")
    app.include_router(runs_router, prefix="/runs")
    app.include_router(settings_router, prefix="/settings")
    app.include_router(unsubscribe_router)
    names: tuple[CapabilityName, ...] = (
        "research",
        "contacts",
        "campaign",
        "reply",
        "sourcing",
        "quotation",
        "inbound_body",
        "full_reply",
        "agent",
        "browser",
    )
    capabilities = (
        resolved_dependencies.runtime_capabilities
        if isinstance(resolved_dependencies, ConfiguredApiDependencies)
        and resolved_dependencies.runtime_capabilities
        else tuple(
            RuntimeCapability(name=name, status="disabled", reason="not_requested")
            for name in names
        )
    )
    app.include_router(build_capability_router(capabilities))
    if readiness_probe is not None:
        app.include_router(build_health_router(readiness_probe))
    _install_openapi_contract(app)
    return app


def main() -> None:
    """以 factory 模式启动；未注入 composition 时所有业务依赖失败关闭。"""
    import uvicorn

    uvicorn.run(
        "apps.api.runtime:create_runtime_app",
        factory=True,
        access_log=False,
    )


if __name__ == "__main__":
    main()
