"""Stage: 频率与配额。

三层限额取最小：
1. 发件身份日限额（sending_identity 的 SendPermission，含预热）
2. Campaign 日限额（outreach 的 QuotaRepository）
3. 工具级全局频率（防 Agent 死循环刷调用）

放最后是因为它最"贵"（要查多个计数器），且幂等命中不该消耗限额。
remediation 带上"什么时候再试"。
"""

from __future__ import annotations

from collections.abc import Callable

from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.schemas import MessageSendPreflight
from domains.outreach.service import OutreachService, SendFailureCategory
from domains.sending_identity.errors import (
    AuthenticationNotVerifiedError,
    ColdOutreachDomainViolation,
    IdentityRetiredError,
    IdentitySuspendedError,
    WarmupLimitExceededError,
)
from domains.sending_identity.permissions import Actor as SendingIdentityActor
from domains.sending_identity.service import SendingIdentityService
from shared.errors import PolicyViolation, TransientError, ValidationError
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState


class RateLimitCheck:
    name = "rate_limit"

    def __init__(
        self,
        outreach: OutreachService,
        sending_identity: SendingIdentityService,
        outreach_actor_provider: Callable[[ToolCallContext], OutreachActor],
        sending_actor_provider: Callable[
            [ToolCallContext, MessageSendPreflight], SendingIdentityActor
        ],
    ) -> None:
        self._outreach = outreach
        self._sending = sending_identity
        self._outreach_actor_provider = outreach_actor_provider
        self._sending_actor_provider = sending_actor_provider

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if not isinstance(state.preflight, MessageSendPreflight):
            return CheckRejection(
                self.name,
                "rate_limit:preflight_missing",
                "发送前事实尚未确认",
            )
        preflight = state.preflight
        try:
            await self._outreach.claim_message_send(
                ctx.tenant_id,
                preflight.attempt_id,
                actor=self._outreach_actor_provider(ctx),
            )
        except (PolicyViolation, ValidationError):
            return CheckRejection(
                self.name,
                "outreach:claim_rejected",
                "发送尝试当前不可认领",
            )
        try:
            await self._sending.reserve_send_slot(
                ctx.tenant_id,
                preflight.sending_identity_id,
                preflight.idempotency_key,
                True,
                actor=self._sending_actor_provider(ctx, preflight),
            )
        except WarmupLimitExceededError:
            await self._record_failure(ctx, preflight, "rate_limited")
            raise ToolGatewayError(ToolErrorCategory.RATE_LIMITED) from None
        except AuthenticationNotVerifiedError:
            await self._record_failure(ctx, preflight, "provider_auth_required")
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED) from None
        except (
            ColdOutreachDomainViolation,
            IdentityRetiredError,
            IdentitySuspendedError,
        ):
            await self._record_failure(ctx, preflight, "identity_unavailable")
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT) from None
        except TransientError:
            await self._record_failure(ctx, preflight, "provider_transient")
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        return None

    async def _record_failure(
        self, ctx: ToolCallContext, preflight: MessageSendPreflight, value: str
    ) -> None:
        await self._outreach.record_send_failure(
            ctx.tenant_id,
            preflight.attempt_id,
            SendFailureCategory(value),
            actor=self._outreach_actor_provider(ctx),
        )

    async def record_sent(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
        provider_ref: str,
    ) -> None:
        if not isinstance(state.preflight, MessageSendPreflight):
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        await self._outreach.record_sent(
            ctx.tenant_id,
            state.preflight.attempt_id,
            provider_ref,
            actor=self._outreach_actor_provider(ctx),
        )
