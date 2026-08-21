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
from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachAction,
    OutreachAuthorizer,
    OutreachScope,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.service import OutreachService
from domains.prospecting.service import ProspectingService
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.permissions import (
    SendingIdentityAction,
    SendingIdentityAuthorizer,
    SendingIdentityScope,
)
from domains.sending_identity.service import SendingIdentityService
from notification_gateway.dedup import NotificationDedupStore
from notification_gateway.inbox import InAppNotificationService, InboxActor
from notification_gateway.router import NotificationRouter
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import CampaignId, TenantId
from tool_gateway.handlers.email_send import (
    DeliveryMaterialProvider,
    UnsubscribeLinkProvider,
)
from tool_gateway.pipeline import ToolCallContext, ToolCallResult
from workflows.email_feedback.unsubscribe import UnsubscribeService
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
    unsubscribe_service: UnsubscribeService
    employees: EmployeeServiceScope
    opportunity_authorizer: OpportunityAuthorizer
    employee_authorizer: EmployeeAuthorizer
    workflow_engine: WorkflowEngine
    outbox_deliverer: OutboxDeliverer
    notification_router: NotificationRouter
    notification_dedup_store: NotificationDedupStore
    outreach_authorizer: OutreachAuthorizer
    sending_identity_authorizer: SendingIdentityAuthorizer
    campaign_scope_resolver: CampaignScopeResolver
    in_app_notifications: InAppNotificationService
    employee_lookup_actor: EmployeeActor
    prospecting: ProspectingService | None = None
    configured: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tool_gateway, ToolGatewayInvoker)
            or not isinstance(self.delivery_materials, DeliveryMaterialProvider)
            or not isinstance(self.unsubscribe_links, UnsubscribeLinkProvider)
            or not isinstance(self.unsubscribe_service, UnsubscribeService)
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


class CampaignScopeResolver(Protocol):
    """把员工（含 manager 直系下属）映射到其创建/管辖的 Campaign 集合。"""

    async def campaign_ids_for(
        self, *, created_by: frozenset[str]
    ) -> frozenset[CampaignId]: ...


async def resolve_outreach_scope(
    identity: RequestIdentity,
    dependencies: ConfiguredApiDependencies,
) -> OutreachScope:
    """按角色与员工机会所有权推导不可变触达作用域。

    boss 直接 TENANT；manager/sales 用机会域已推导的 allowed_owners
    （self + manager 直系下属）解析其创建/管辖的 Campaign 集合。
    """
    if identity.employee.role == "boss":
        return OutreachScope(level=OutreachScopeLevel.TENANT)
    owners = identity.opportunity_actor.scope.allowed_owners
    created_by = frozenset(str(owner) for owner in (owners or frozenset()))
    campaign_ids = await dependencies.campaign_scope_resolver.campaign_ids_for(
        created_by=created_by
    )
    level = (
        OutreachScopeLevel.MANAGER
        if identity.employee.role == "manager"
        else OutreachScopeLevel.SELF
    )
    return OutreachScope(level=level, allowed_campaign_ids=campaign_ids)


async def resolve_outreach_access(
    identity: RequestIdentity,
    dependencies: ConfiguredApiDependencies,
    action: OutreachAction,
    *,
    allowed_roles: frozenset[str],
) -> OutreachActor:
    """第一道触达门：角色 + authorizer preauthorize；返回派生 actor 供域调用。"""
    if not isinstance(action, OutreachAction) or not allowed_roles:
        raise ValidationError("API outreach gate 必须显式声明 typed action 与角色")
    if identity.employee.role not in allowed_roles:
        raise PermissionDenied("API role gate 默认拒绝")
    scope = await resolve_outreach_scope(identity, dependencies)
    actor = OutreachActor(
        str(identity.employee.employee_id), scope, identity.employee.role
    )
    dependencies.outreach_authorizer.preauthorize(
        actor, action, scope, identity.tenant_id
    )
    return actor


def sending_identity_actor_for(identity: RequestIdentity) -> SendingIdentityActor:
    """boss 的唯一发件身份读取角色；scope 恒为 TENANT。"""
    return SendingIdentityActor(
        str(identity.employee.employee_id),
        SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
        identity.employee.role,
    )


async def resolve_sending_identity_access(
    identity: RequestIdentity,
    dependencies: ConfiguredApiDependencies,
    action: SendingIdentityAction,
    *,
    allowed_roles: frozenset[str],
) -> SendingIdentityActor:
    """第一道发件身份门：角色 + authorizer preauthorize。"""
    if not isinstance(action, SendingIdentityAction) or not allowed_roles:
        raise ValidationError(
            "API sending identity gate 必须显式声明 typed action 与角色"
        )
    if identity.employee.role not in allowed_roles:
        raise PermissionDenied("API role gate 默认拒绝")
    actor = sending_identity_actor_for(identity)
    dependencies.sending_identity_authorizer.preauthorize(
        actor, action, actor.scope, identity.tenant_id
    )
    return actor


def require_inbox_access(
    identity: RequestIdentity,
    *,
    allowed_roles: frozenset[str],
) -> InboxActor:
    """收件箱第一道门：任何已知员工角色；收件人恒为身份本人。"""
    if not allowed_roles:
        raise ValidationError("API inbox gate 必须显式声明角色")
    if identity.employee.role not in allowed_roles:
        raise PermissionDenied("API role gate 默认拒绝")
    return InboxActor(identity.tenant_id, identity.employee.employee_id)
