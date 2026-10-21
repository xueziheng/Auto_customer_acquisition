"""原 model.generate 的受控预算包装；不建立模型连接或替代全局额度。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol

from connectors.codex_agent.client import AgentProvider
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelLimits,
    ModelRequest,
    ModelResponse,
)
from shared.schemas.qq_trial import QQTrialGate, QQTrialStopped
from tool_gateway.checks.model import CurrentModelAuthority
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import GatewayModelGenerator, ModelProvider
from tool_gateway.model_usage import ModelUsageRepository
from tool_gateway.repository import ToolGatewayUnitOfWorkFactory

logger = logging.getLogger(__name__)


async def _stop(gate: QQTrialGate) -> None:
    try:
        await asyncio.shield(gate.stop("failed_or_unknown"))
    except BaseException:  # noqa: BLE001 - 停止审计失败不能遮蔽原取消/未知结果
        # 预留已独立提交；清理失败也不能遮蔽原异常或返还额度。
        logger.error("QQ 验收停止记录失败；保留已预留额度")


@dataclass(frozen=True)
class TrialModelPorts:
    authority: CurrentModelAuthority
    usage: ModelUsageRepository
    ledger_factory: ToolGatewayUnitOfWorkFactory
    fingerprints: HmacFingerprintProvider
    limits: ModelLimits
    lease_owner: str
    lease_duration: timedelta
    provider: str


class TrialModelBudget(QQTrialGate, Protocol):
    async def require_message(self, message_id: str) -> None: ...
    async def remaining_output(self) -> int: ...
    async def provider_finished(self, tokens: int, requested: int) -> None: ...


class TrialModelGenerator:
    """必须成对注入下述 Provider 轮次守卫；仍经过原 Gateway 身份与总额度。"""

    def __init__(
        self,
        gate: TrialModelBudget,
        *,
        message_id: str,
        ports: TrialModelPorts,
        provider_factory: Callable[
            ["TrialProviderBudget", InvocationIdentity], ModelProvider
        ],
    ) -> None:
        self.gate, self.message_id, self.ports = gate, message_id, ports
        self.provider_budget = TrialProviderBudget(gate)
        self.provider_factory = provider_factory

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest
    ) -> ModelResponse:
        scope = self.gate.scope
        if (
            identity.tenant_id != scope.route.tenant_id
            or identity.employee_id != scope.classifier_employee_id
            or identity.user_id != scope.classifier_user_id
            or identity.configuration_version != scope.model_configuration_version
            or identity.capability != "reply_qualification"
            or identity.sequence != 0
            or identity.turn_id is not None
            or request.model != scope.model
            or request.images
        ):
            raise QQTrialStopped("model_binding")
        size = len(
            json.dumps(
                {
                    "model": request.model,
                    "instructions": request.system_prompt,
                    "input": request.payload,
                },
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        )
        if size > scope.limits.model_input_bytes:
            raise QQTrialStopped("model_input_limit")
        await self.ports.authority.check(identity)
        await self.gate.require_message(self.message_id)
        await self.gate.reserve("classifications")
        bounded = request.model_copy(
            update={
                "max_output_tokens": min(
                    request.max_output_tokens,
                    scope.limits.model_output_tokens,
                    self.ports.limits.max_output_tokens,
                )
            }
        )

        def guarded_provider() -> ModelProvider:
            provider = self.provider_factory(self.provider_budget, identity)
            # 在 prepare/generate 前拒绝漏接，不能派发后才察觉预算被绕过。
            if not isinstance(
                provider, AgentProvider
            ) or not provider.uses_round_budget(self.provider_budget):
                raise QQTrialStopped("provider_budget_missing")
            return provider

        inner = GatewayModelGenerator(
            authority=self.ports.authority,
            usage=self.ports.usage,
            provider_factory=guarded_provider,
            limits=self.ports.limits,
            model=scope.model,
            configuration_version=scope.model_configuration_version,
            provider=self.ports.provider,
            ledger_factory=self.ports.ledger_factory,
            fingerprints=self.ports.fingerprints,
            lease_owner=self.ports.lease_owner,
            lease_duration=self.ports.lease_duration,
        )
        try:
            async with asyncio.timeout(
                min(
                    scope.limits.model_timeout_seconds,
                    self.ports.limits.timeout_seconds,
                )
            ):
                result = await inner.generate(identity, bounded)
            # 未经过逐轮守卫的端口不能宣称完成本次受控调用。
            if self.provider_budget.completed == 0:
                raise QQTrialStopped("provider_budget_bypassed")
            await self.ports.authority.check(identity)
            await self.gate.require_message(self.message_id)
            return result
        except BaseException:
            await _stop(self.gate)
            raise


class TrialMessageGenerators:
    """到持久消息已确定时才建立请求包装，不在进程启动时猜 message_id。"""
    def __init__(self, gate: TrialModelBudget, ports: TrialModelPorts,
                 provider_factory: Callable[["TrialProviderBudget", InvocationIdentity], ModelProvider]) -> None:
        self.gate, self.ports, self.provider_factory = gate, ports, provider_factory

    async def generate(self, identity: InvocationIdentity, request: ModelRequest) -> ModelResponse:
        raise QQTrialStopped("message_binding")

    def bind_message(self, message_id: str) -> TrialModelGenerator:
        return TrialModelGenerator(self.gate,message_id=message_id,ports=self.ports,
            provider_factory=self.provider_factory)


class TrialProviderBudget:
    """在现有 broker 真正 round 前后调用；每轮累计输入、输出，未知不退款。"""

    def __init__(self, gate: TrialModelBudget) -> None:
        self.gate, self.completed = gate, 0

    async def run(
        self,
        body: dict,
        dispatch: Callable[[dict], Awaitable[dict]],
        *,
        output_key: Literal["max_tokens", "max_output_tokens"] = "max_output_tokens",
    ) -> dict:
        try:
            await self.gate.check()
            output = body.get(output_key)
            if (
                type(output) is not int
                or output < 1
                or body.get("model") != self.gate.scope.model
            ):
                raise QQTrialStopped("provider_request_invalid")
            size = len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode())
            await self.gate.reserve("provider_requests", input_bytes=size)
            bound = min(output, await self.gate.remaining_output())
            await self.gate.check()
            response = await dispatch({**body, output_key: bound})
            tokens = response.get("usage", {}).get("output_tokens")
            await self.gate.provider_finished(tokens, bound)
            self.completed += 1
            return response
        except BaseException:
            await _stop(self.gate)
            raise
