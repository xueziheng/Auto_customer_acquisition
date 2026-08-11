"""Stage: RBAC + ABAC。

RBAC：用户角色是否覆盖 manifest.required_permissions。
ABAC：参数引用的业务对象是否在用户范围内（国家、归属客户、Campaign）。

这是第二道判权（第一道在域服务层）。两道都要有：Gateway 是
「上游漏了也能兜住」的最后一道闸。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from shared.errors import PermissionDenied
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState


class PermissionCheck:
    name = "permission"

    def __init__(
        self,
        authorize: Callable[
            [ToolCallContext, ToolInvocationState], Awaitable[bool]
        ],
    ) -> None:
        self._authorize = authorize

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        try:
            allowed = await self._authorize(ctx, state)
        except PermissionDenied:
            allowed = False
        if allowed is not True:
            return CheckRejection(
                self.name,
                "permission:denied",
                "当前操作者无权执行此工具",
            )
        return None
