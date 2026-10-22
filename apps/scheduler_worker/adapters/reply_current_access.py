"""受托回复只从受信员工查询取得当前资格；system 不授予内容读取权限。"""

from collections.abc import Callable
from dataclasses import dataclass

from apps.composition_support.employee_readers import EmployeeServiceScope
from connectors.qq_mail.inbound import ProjectReplyScope
from domains.assistant.schemas import AssistantActor
from domains.assistant.service import ModelConfigurationService
from domains.conversations.dialogue_access import OrdinaryDialogueAccess
from domains.conversations.service import (
    ConversationsUnitOfWork,
    InboxActor,
    InboxScope,
    require_reply_internal_access,
)
from domains.employees.errors import EmployeeNotFoundError
from domains.employees.permissions import Actor, EmployeeScope
from infra.db.reply_retry import SqlReplyRetryRepository
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    OutboundMessageId,
    TenantId,
)
from shared.schemas.qq_trial import QQTrialGate, QQTrialScope
from shared.schemas.quote_facts import QuoteEmployeeFact


@dataclass(frozen=True)
class QQTrialCurrentAccess:
    """拉取前也使用原邮箱事实和模型配置公开授权；不等待已有消息才能查许可。"""

    scope: QQTrialScope
    mailbox: ProjectReplyScope
    configuration: ModelConfigurationService

    async def __call__(self) -> bool:
        scope = self.scope
        target = await self.mailbox.target(scope.route, scope.employee_id)
        if (
            target is None
            or target.attempt_id != scope.attempt_id
            or target.outbound_message_header != scope.canonical_header
            or (target.sender, target.recipient) != (scope.sender, scope.recipient)
        ):
            return False
        actor = AssistantActor(
            tenant_id=scope.route.tenant_id,
            employee_id=scope.classifier_employee_id,
            user_id=scope.classifier_user_id,
        )
        await self.configuration.authorize(
            actor, scope.model_configuration_version, probe=False
        )
        view = await self.configuration.get_public(actor)
        return (
            view.status == "verified"
            and view.model == scope.model
            and view.configuration_version == scope.model_configuration_version
        )


@dataclass(frozen=True)
class CurrentReplyAccess:
    """配置只是标识绑定；每次检查打开独立当前员工 scope，不缓存角色。"""

    tenant_id: TenantId
    employee_id: EmployeeId | None
    employee_scope: EmployeeServiceScope
    uow_factory: Callable[[TenantId], ConversationsUnitOfWork]
    trial_gate: QQTrialGate | None = None
    retries: SqlReplyRetryRepository | None = None
    recovery_only: bool = False
    dialogue_access: OrdinaryDialogueAccess | None = None

    async def actor(self, tenant_id: TenantId) -> InboxActor:
        """读当前 Employee 后执行原 qualify 纯规则，再构造 Inbox 上界。"""
        if tenant_id != self.tenant_id or self.employee_id is None:
            raise PermissionDenied("回复内部操作权限拒绝")
        try:
            async with self.employee_scope(tenant_id) as employees:
                employee = await employees.get_employee(
                    tenant_id,
                    self.employee_id,
                    actor=Actor("system:reply-actor", EmployeeScope.SYSTEM, "system"),
                )
        except EmployeeNotFoundError:
            raise PermissionDenied("回复内部操作权限拒绝") from None
        fact = QuoteEmployeeFact.model_validate(
            {
                "tenant_id": employee.tenant_id,
                "employee_id": employee.employee_id,
                "role": employee.role,
                "is_active": employee.is_active,
                "manager_id": employee.manager_id,
                "team_id": employee.team_id,
            }
        )
        require_reply_internal_access(tenant_id, fact, action="qualify")
        return InboxActor(tenant_id, self.employee_id, employee.role, InboxScope.TENANT)

    async def for_message(self, tenant_id: TenantId, message_id: MessageId) -> InboxActor:
        """仅服务器精确来源关联可选独立对话入口；普通冷回复仍走老板规则。"""
        async with self.uow_factory(tenant_id) as uow:
            message = await uow.messages.get(tenant_id, message_id)
        if message is None:
            raise PermissionDenied("回复消息绑定拒绝")
        return await self.require(tenant_id, message_id, message.outbound_message_id)

    async def require(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        outbound_message_id: OutboundMessageId | None,
    ) -> InboxActor:
        """读取/模型前检查真实同租户入站与原出站关联，拒绝 context 替换消息。"""
        if self.retries is not None and await self.retries.exists(
            tenant_id, message_id
        ):
            await self.retries.require_message(tenant_id, message_id)
        elif self.recovery_only:
            raise PermissionDenied("该消息没有人工恢复授权")
        elif self.trial_gate is not None:
            await self.trial_gate.require_message(str(message_id))
        if tenant_id != self.tenant_id:
            raise PermissionDenied("回复内部操作权限拒绝")
        async with self.uow_factory(tenant_id) as uow:
            message = await uow.messages.get(tenant_id, message_id)
            reader = getattr(uow, "dialogue_source", None)
            source = await reader(tenant_id, message_id) if callable(reader) else None
        if (
            message is None
            or message.tenant_id != tenant_id
            or message.message_id != message_id
            or message.direction.value != "inbound"
            or (message.outbound_message_id is None and (source is None or source.source_kind != "inquiry"))
            or message.outbound_message_id != outbound_message_id
        ):
            raise PermissionDenied("回复消息绑定拒绝")
        if source is not None:
            if self.dialogue_access is None:
                raise PermissionDenied("普通对话授权未配置")
            return await self.dialogue_access.require(tenant_id, message_id, source.owner_employee_id)
        return await self.actor(tenant_id)
