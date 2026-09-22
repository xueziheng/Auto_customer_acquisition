"""入站只读路由检查；权限继续由受信当前身份authorizer判断。"""

from shared.schemas.email_inbound import InboundRoute
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState


class InboundTenantCheck:
    name = "tenant"

    def __init__(self, route: InboundRoute) -> None:
        self._route = route

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        if (
            ctx.tool_id != "email.inbound.fetch"
            or ctx.tenant_id != self._route.tenant_id
            or ctx.params.get("mailbox_alias") != self._route.mailbox_alias
        ):
            return CheckRejection(
                self.name, "tenant:resource_binding", "入站路由绑定无效"
            )
        return None
