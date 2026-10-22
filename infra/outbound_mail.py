"""本人邮箱连接器的受信部署适配；凭证只在原 Gateway 执行阶段解析。"""
from collections.abc import Callable
from typing import Protocol

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.aliyun_connection import AliyunActor
from infra.aliyun_mail_store import AliyunMailStore
from infra.db.tables import EmployeeRow
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.handlers.email_send import OutboundMailConnector, OutboundMailOwner


def _outbound_connector(address: str, password: SecretStr) -> OutboundMailConnector:
    from connectors.aliyun_mail.client import AliyunMailConnector
    return AliyunMailConnector(address, password)


class GmailOutboundProvider(Protocol):
    async def outbound_connector(self, tenant_id: TenantId, employee_id: EmployeeId, from_address: str) -> OutboundMailConnector | None: ...


class AliyunOutboundConnectors:
    """发送/对账时重核当前员工，仅解析其本人已连接的精确邮箱。"""

    def __init__(self, store: AliyunMailStore, sessions: async_sessionmaker[AsyncSession], *,
        connector_factory: Callable[[str, SecretStr], OutboundMailConnector] = _outbound_connector):
        self.store, self.sessions, self.connector_factory = store, sessions, connector_factory

    async def resolve(self, tenant_id: TenantId, employee_id: EmployeeId, from_address: str, *, expected_operation: str | None = None) -> OutboundMailConnector:
        async with self.sessions() as s:
            user_id = await s.scalar(select(EmployeeRow.user_id).where(
                EmployeeRow.tenant_id == tenant_id, EmployeeRow.employee_id == employee_id,
                EmployeeRow.is_active.is_(True), EmployeeRow.role.in_(("boss", "manager", "sales")),
            ))
        if user_id is None:
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
        actor = AliyunActor(tenant_id=tenant_id, employee_id=employee_id, user_id=UserId(user_id))
        binding = self.store.binding(actor)
        if binding is None or binding.email != from_address or (expected_operation is not None and binding.operation_id != expected_operation):
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        return self.connector_factory(binding.email, binding.password)


class EmployeeMailConnectors:
    def __init__(self, gmail: GmailOutboundProvider | None, aliyun: AliyunOutboundConnectors | None,
                 qq: AliyunOutboundConnectors | None = None):
        self.gmail, self.aliyun, self.qq = gmail, aliyun, qq

    async def resolve_owned(self, owner: OutboundMailOwner, from_address: str) -> OutboundMailConnector:
        """仅使用当前已校验的原 QQ 绑定；一次读取后核对操作版本再创建连接器。"""
        if self.qq is None or not from_address.endswith("@qq.com") or owner.proof[1] != from_address:
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
        return await self.qq.resolve(owner.tenant_id, owner.employee_id, from_address, expected_operation=owner.proof[5])

    async def resolve(self, tenant_id: TenantId, employee_id: EmployeeId, from_address: str) -> OutboundMailConnector:
        """发送执行或只读对账时按当前员工和地址选路；解析失败直接传播。"""
        if from_address.endswith("@qq.com"):
            if self.qq is None:
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
            return await self.qq.resolve(tenant_id, employee_id, from_address)
        if self.gmail is not None:
            connector = await self.gmail.outbound_connector(tenant_id, employee_id, from_address)
            if connector is not None:
                return connector
        if self.aliyun is not None:
            return await self.aliyun.resolve(tenant_id, employee_id, from_address)
        raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
