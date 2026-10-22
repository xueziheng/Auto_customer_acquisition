"""受信本人邮箱选路；只消费精确绑定，不按失败切换发件身份。"""
from shared.schemas.identifiers import EmployeeId, TenantId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.handlers.email_send import OutboundMailConnector

from .aliyun_web import AliyunOutboundConnectors
from .gmail_web import GmailWebService


class EmployeeMailConnectors:
    def __init__(self, gmail: GmailWebService | None, aliyun: AliyunOutboundConnectors | None,
                 qq: AliyunOutboundConnectors | None = None):
        self.gmail, self.aliyun, self.qq = gmail, aliyun, qq

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
