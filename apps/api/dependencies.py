"""API 依赖容器、request scope 获取器与第一道 typed action gate。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Annotated, Protocol, runtime_checkable

from fastapi import Depends, Request

from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    EmployeeAction,
    EmployeeAuthorizer,
    EmployeeScope,
)
from domains.employees.service import EmployeeService
from domains.opportunities.permissions import (
    OpportunityAction,
    OpportunityAuthorizer,
)
from domains.opportunities.service import OpportunityService
from domains.outreach.service import OutreachService
from domains.sending_identity.service import SendingIdentityService
from notification_gateway.dedup import NotificationDedupStore
from notification_gateway.router import NotificationRouter
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.handlers.email_send import (
    DeliveryMaterialProvider,
    UnsubscribeLinkProvider,
)
from tool_gateway.pipeline import ToolCallContext, ToolCallResult
from workflows.engine.runner import WorkflowEngine

from .middleware import ApiSettings


class EmployeeServiceScope(Protocol):
    """每次调用创建并托管一个 request-scoped 公共员工服务。"""

    def __call__(
        self, tenant_id: TenantId
    ) -> AbstractAsyncContextManager[EmployeeService]: ...


class OutboxDeliverer(Protocol):
    """API composition 所需 outbox 投递器的最窄公共能力。"""

    async def drain(self) -> int: ...


@runtime_checkable
class ToolGatewayInvoker(Protocol):
    """API 只依赖工具网关的单一调用入口。"""

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


@dataclass(frozen=True)
class ConfiguredApiDependencies:
    """完整且已配置的 API runtime 依赖。

    员工服务必须通过 ``employees`` 每请求创建，禁止把绑定单个 SQLAlchemy
    session 的实现挂成 app singleton。
    """

    opportunities: OpportunityService
    outreach: OutreachService
    sending_identities: SendingIdentityService
    tool_gateway: ToolGatewayInvoker
    delivery_materials: DeliveryMaterialProvider
    unsubscribe_links: UnsubscribeLinkProvider
    employees: EmployeeServiceScope
    opportunity_authorizer: OpportunityAuthorizer
    employee_authorizer: EmployeeAuthorizer
    workflow_engine: WorkflowEngine
    outbox_deliverer: OutboxDeliverer
    notification_router: NotificationRouter
    notification_dedup_store: NotificationDedupStore
    employee_lookup_actor: EmployeeActor
    configured: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tool_gateway, ToolGatewayInvoker)
            or not isinstance(self.delivery_materials, DeliveryMaterialProvider)
            or not isinstance(self.unsubscribe_links, UnsubscribeLinkProvider)
        ):
            raise TypeError("API 手工发送依赖未完整配置")
        actor = self.employee_lookup_actor
        if (
            actor.scope is not EmployeeScope.SYSTEM
            or actor.role != "system"
            or not actor.actor_id
            or not actor.actor_id.strip()
        ):
            raise ValueError("employee lookup actor 必须是显式最小 SYSTEM actor")


@dataclass(frozen=True)
class UnconfiguredApiDependencies:
    """zero-arg 工厂的固定未配置态；不持有任何空 handler 或外部资源。"""

    configured: bool = False
    reason_code: str = "API_DEPENDENCIES_NOT_CONFIGURED"


ApiDependencies = ConfiguredApiDependencies | UnconfiguredApiDependencies


def get_api_settings(request: Request) -> ApiSettings:
    """取得当前 app 的冻结配置。"""
    settings = request.app.state.settings
    if not isinstance(settings, ApiSettings):
        raise TransientError("API 配置不可用")
    return settings


def get_api_dependencies(request: Request) -> ConfiguredApiDependencies:
    """取得完整依赖；未配置 app 固定 503，未来 endpoint 不得用空依赖运行。"""
    dependencies = request.app.state.dependencies
    if not isinstance(dependencies, ConfiguredApiDependencies):
        raise TransientError("API runtime 尚未配置")
    return dependencies


# identity 仅在本模块容器契约定义完成后单向导入；identity 的反向边只用于类型检查。
from .identity import RequestIdentity, resolve_request_identity


async def get_request_identity(
    request: Request,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> RequestIdentity:
    """经 dev assertion 和 public EmployeeService DTO 解析请求身份。"""
    return await resolve_request_identity(request, settings, dependencies)


def require_opportunity_action(
    action: OpportunityAction,
    *,
    allowed_roles: frozenset[str],
):
    """构造机会域第一道 role/action gate；服务层 authorizer 仍须二次判权。"""
    if not isinstance(action, OpportunityAction) or not allowed_roles:
        raise ValidationError("API opportunity gate 必须显式声明 typed action 与角色")

    async def gate(
        identity: Annotated[RequestIdentity, Depends(get_request_identity)],
        dependencies: Annotated[
            ConfiguredApiDependencies, Depends(get_api_dependencies)
        ],
    ) -> RequestIdentity:
        if identity.employee.role not in allowed_roles:
            raise PermissionDenied("API role gate 默认拒绝")
        dependencies.opportunity_authorizer.require(
            identity.opportunity_actor,
            action,
            identity.opportunity_actor.scope,
            identity.tenant_id,
        )
        return identity

    return gate


def require_employee_action(
    action: EmployeeAction,
    *,
    allowed_roles: frozenset[str],
):
    """构造员工域第一道 role/action gate；不替代员工服务内部 authorizer。"""
    if not isinstance(action, EmployeeAction) or not allowed_roles:
        raise ValidationError("API employee gate 必须显式声明 typed action 与角色")

    async def gate(
        identity: Annotated[RequestIdentity, Depends(get_request_identity)],
        dependencies: Annotated[
            ConfiguredApiDependencies, Depends(get_api_dependencies)
        ],
    ) -> RequestIdentity:
        if identity.employee.role not in allowed_roles:
            raise PermissionDenied("API role gate 默认拒绝")
        dependencies.employee_authorizer.require(
            identity.employee_actor,
            action,
            identity.employee_actor.scope,
            identity.tenant_id,
        )
        return identity

    return gate
