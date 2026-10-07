"""受托回复只从受信员工查询取得当前资格；system 不授予内容读取权限。"""

from collections.abc import Callable
from dataclasses import dataclass

from apps.composition_support.employee_readers import EmployeeServiceScope
from domains.conversations.service import (
    ConversationsUnitOfWork,
    InboxActor,
    InboxScope,
    require_reply_internal_access,
)
from domains.employees.errors import EmployeeNotFoundError
from domains.employees.permissions import Actor, EmployeeScope
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    OutboundMessageId,
    TenantId,
)
from shared.schemas.quote_facts import QuoteEmployeeFact


@dataclass(frozen=True)
class CurrentReplyAccess:
    """配置只是标识绑定；每次检查打开独立当前员工 scope，不缓存角色。"""

    tenant_id: TenantId
    employee_id: EmployeeId
    employee_scope: EmployeeServiceScope
    uow_factory: Callable[[TenantId], ConversationsUnitOfWork]

    async def actor(self, tenant_id: TenantId) -> InboxActor:
        """读当前 Employee 后执行原 qualify 纯规则，再构造 Inbox 上界。"""
        if tenant_id != self.tenant_id:
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

    async def require(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        outbound_message_id: OutboundMessageId | None,
    ) -> InboxActor:
        """读取/模型前检查真实同租户入站与原出站关联，拒绝 context 替换消息。"""
        await self.actor(tenant_id)
        async with self.uow_factory(tenant_id) as uow:
            message = await uow.messages.get(tenant_id, message_id)
        if (
            message is None
            or message.tenant_id != tenant_id
            or message.message_id != message_id
            or message.direction.value != "inbound"
            or message.outbound_message_id is None
            or message.outbound_message_id != outbound_message_id
        ):
            raise PermissionDenied("回复消息绑定拒绝")
        return await self.actor(tenant_id)
