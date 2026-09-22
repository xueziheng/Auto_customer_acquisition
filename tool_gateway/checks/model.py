"""模型插件的受信身份、当前许可和技术额度检查。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelFailureCode,
    ModelGenerationError,
    ModelLimits,
)
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.model_usage import ModelUsageRepository, Reservation
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState


class CurrentModelAuthority(Protocol):
    async def check(self, identity: InvocationIdentity) -> None:
        """核验当前员工、Run 归属、模型外发许可和精确配置版本。"""
        ...


class ModelIdentityCheck:
    def __init__(
        self, name: str, identity: InvocationIdentity, authority: CurrentModelAuthority
    ) -> None:
        self.name, self._identity, self._authority = name, identity, authority
        self.failure: ModelGenerationError | None = None

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if (
            ctx.tenant_id != self._identity.tenant_id
            or ctx.user_id != self._identity.user_id
            or ctx.run_id != self._identity.run_id
        ):
            self.failure = ModelGenerationError("permission")
        elif self.name != "tenant":
            try:
                await self._authority.check(self._identity)
            except ModelGenerationError as exc:
                self.failure = exc
        if self.failure:
            return CheckRejection(self.name, "model:authority", "当前模型调用未获许可")
        return None


class ModelQuotaCheck:
    name = "rate_limit"

    def __init__(
        self,
        identity: InvocationIdentity,
        usage: ModelUsageRepository,
        limits: ModelLimits,
        model: str,
        now: Callable[[], datetime],
    ) -> None:
        self._identity, self._usage, self._limits, self._model, self._now = (
            identity,
            usage,
            limits,
            model,
            now,
        )
        self.reservation: Reservation | None = None
        self.failure: ModelGenerationError | None = None

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if state.prepared is None or ctx.tenant_id != self._identity.tenant_id:
            return CheckRejection(self.name, "model:unprepared", "模型调用材料缺失")
        self.reservation = await self._usage.reserve(
            self._identity,
            state.prepared.request_fingerprint,
            self._limits,
            self._now(),
            model=self._model,
        )
        if self.reservation.outcome != "reserved":
            code: ModelFailureCode = (
                "quota" if self.reservation.outcome == "limited" else "unknown"
            )
            self.failure = ModelGenerationError(code)
            # 此阶段已完成 canonical claim；按失败结算，不能退回 RECEIVED 拒绝态。
            raise ToolGatewayError(
                ToolErrorCategory.RATE_LIMITED
                if code == "quota"
                else ToolErrorCategory.RECONCILIATION_REQUIRED
            )
        return None
