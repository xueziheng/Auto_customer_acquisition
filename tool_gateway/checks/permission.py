"""Stage: RBAC + ABAC。

RBAC：用户角色是否覆盖 manifest.required_permissions。
ABAC：参数引用的业务对象是否在用户范围内（国家、归属客户、Campaign）。

这是第二道判权（第一道在域服务层）。两道都要有：Gateway 是
「上游漏了也能兜住」的最后一道闸。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class PermissionCheck:
    name = "permission"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
