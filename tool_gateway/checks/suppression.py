"""Stage: 抑制名单。

联系人级和企业级都查（经 domains/outreach 的 service）。

**服务不可用时拒绝发送，不是放行**——宁可晚发一封，
不能发给退订过的人。remediation 恒为 None。
"""

from __future__ import annotations

from collections.abc import Callable

from domains.outreach.permissions import Actor
from domains.outreach.service import OutreachService
from shared.errors import PolicyViolation, TransientError, ValidationError
from shared.schemas.identifiers import MessageAttemptId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState


class SuppressionCheck:
    name = "suppression"

    def __init__(
        self,
        outreach: OutreachService,
        actor_provider: Callable[[ToolCallContext], Actor],
    ) -> None:
        self._outreach = outreach
        self._actor_provider = actor_provider

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        attempt_id = ctx.params.get("attempt_id")
        if not isinstance(attempt_id, str):
            return CheckRejection(
                self.name,
                "outreach:attempt_binding",
                "发送尝试绑定无效",
            )
        try:
            state.preflight = await self._outreach.preflight_message_send(
                ctx.tenant_id,
                MessageAttemptId(attempt_id),
                actor=self._actor_provider(ctx),
            )
        except TransientError:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        except (PolicyViolation, ValidationError):
            return CheckRejection(
                self.name,
                "outreach:current_fact",
                "当前触达事实不允许发送",
            )
        return None
