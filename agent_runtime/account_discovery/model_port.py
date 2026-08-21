"""AccountDiscoveryAgent 的统一结构化模型端口适配。"""

from __future__ import annotations

from agent_runtime.model_client import StructuredJsonModelClient
from shared.errors import ValidationError


class StructuredAccountDiscoveryModelPort:
    """把账户发现的固定用途映射到统一 JSON 模型客户端。"""

    def __init__(
        self,
        client: StructuredJsonModelClient,
        model: str,
        *,
        max_output_tokens: int = 1_500,
    ) -> None:
        if (
            not isinstance(client, StructuredJsonModelClient)
            or not isinstance(model, str)
            or not model
            or model != model.strip()
            or type(max_output_tokens) is not int
            or not 64 <= max_output_tokens <= 4_096
        ):
            raise ValidationError("账户发现模型配置无效")
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def discover_account(
        self, *, system_prompt: str, hypothesis: dict[str, object]
    ) -> str:
        return await self._client.complete_json(
            model=self._model,
            system_prompt=system_prompt,
            payload=hypothesis,
            max_output_tokens=self._max_output_tokens,
        )


__all__ = ("StructuredAccountDiscoveryModelPort",)
