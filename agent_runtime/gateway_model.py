"""把既有窄模型端口绑定到一次受信、持久编号的网关调用。"""

from collections.abc import Mapping

from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationPort,
    ModelRequest,
)


class GatewayJsonModelClient:
    def __init__(
        self, identity: InvocationIdentity, generator: ModelGenerationPort
    ) -> None:
        self._identity = identity
        self._generator = generator

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        """编号不在内存递增；重试继续使用相同调用身份。"""
        response = await self._generator.generate(
            self._identity,
            ModelRequest(
                model=model,
                system_prompt=system_prompt,
                payload=dict(payload),
                max_output_tokens=max_output_tokens,
            ),
        )
        return response.text
