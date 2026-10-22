"""显式人工恢复身份的原 model.generate 包装；默认序号零路径不变。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from connectors.codex_agent.client import AgentProvider
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelRequest,
    ModelResponse,
)
from tool_gateway.handlers.model_generate import GatewayModelGenerator, ModelProvider
from tool_gateway.qq_trial_model import TrialModelPorts


class ReplyRetryBudget(Protocol):
    async def check(self) -> None: ...
    async def start(self) -> None: ...
    async def reserve_round(self, input_bytes: int) -> int: ...
    async def finish_round(self, tokens: int, requested: int) -> None: ...
    async def fail(self) -> None: ...


class ReplyRetryProviderBudget:
    def __init__(self, gate: ReplyRetryBudget, model: str) -> None:
        self.gate, self.model, self.completed = gate, model, 0

    async def run(
        self,
        body: dict,
        dispatch: Callable[[dict], Awaitable[dict]],
        *,
        output_key: Literal["max_tokens", "max_output_tokens"] = "max_output_tokens",
    ) -> dict:
        await self.gate.check()
        output = body.get(output_key)
        if type(output) is not int or output < 1 or body.get("model") != self.model:
            raise ModelGenerationError("permission")
        size = len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode())
        remaining = await self.gate.reserve_round(size)
        bound = min(output, remaining)
        await self.gate.check()
        response = await dispatch({**body, output_key: bound})
        await self.gate.finish_round(
            response.get("usage", {}).get("output_tokens"), bound
        )
        self.completed += 1
        return response


class ReplyRetryModelGenerator:
    def __init__(
        self,
        identity: InvocationIdentity,
        model: str,
        gate: ReplyRetryBudget,
        ports: TrialModelPorts,
        provider_factory: Callable[
            [ReplyRetryProviderBudget, InvocationIdentity], ModelProvider
        ],
    ) -> None:
        self.identity, self.model, self.gate, self.ports = identity, model, gate, ports
        self.budget = ReplyRetryProviderBudget(gate, model)
        self.provider_factory = provider_factory

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest
    ) -> ModelResponse:
        if (
            identity != self.identity
            or identity.sequence < 1
            or identity.capability != "reply_qualification"
            or identity.turn_id is not None
            or request.model != self.model
            or request.images
        ):
            raise ModelGenerationError("permission")
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
        if size > min(32768, self.ports.limits.max_input_bytes):
            raise ModelGenerationError("permission")
        await self.ports.authority.check(identity)
        await self.gate.start()
        bounded = request.model_copy(
            update={
                "max_output_tokens": min(
                    request.max_output_tokens, 512, self.ports.limits.max_output_tokens
                )
            }
        )

        def provider():
            result = self.provider_factory(self.budget, identity)
            if not isinstance(result, AgentProvider) or not result.uses_round_budget(
                self.budget
            ):
                raise ModelGenerationError("configuration")
            return result

        inner = GatewayModelGenerator(
            authority=self.ports.authority,
            usage=self.ports.usage,
            provider_factory=provider,
            limits=self.ports.limits,
            model=self.model,
            provider=self.ports.provider,
            configuration_version=identity.configuration_version,
            ledger_factory=self.ports.ledger_factory,
            fingerprints=self.ports.fingerprints,
            lease_owner=self.ports.lease_owner,
            lease_duration=self.ports.lease_duration,
        )
        try:
            async with asyncio.timeout(min(60, self.ports.limits.timeout_seconds)):
                result = await inner.generate(identity, bounded)
            if self.budget.completed == 0:
                raise ModelGenerationError("permission")
            await self.ports.authority.check(identity)
            await self.gate.check()
            return result
        except BaseException:
            try:
                await asyncio.shield(self.gate.fail())
            except BaseException:  # noqa: BLE001 - 失败审计不能遮蔽取消或未知结果
                logging.getLogger(__name__).error("回复恢复失败标记未完成；保留预算")
            raise
