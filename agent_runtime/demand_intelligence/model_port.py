"""DemandIntelligenceAgent 的统一结构化模型端口适配。"""

from __future__ import annotations

from agent_runtime.model_client import StructuredJsonModelClient
from shared.errors import ValidationError


class StructuredDemandIntelligenceModelPort:
    def __init__(
        self,
        client: StructuredJsonModelClient,
        model: str,
        *,
        max_output_tokens: int = 3_000,
    ) -> None:
        if (
            not isinstance(client, StructuredJsonModelClient)
            or not isinstance(model, str)
            or not model
            or model != model.strip()
            or type(max_output_tokens) is not int
            or not 128 <= max_output_tokens <= 8_192
        ):
            raise ValidationError("需求情报模型配置无效")
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def analyze_pages(
        self,
        *,
        system_prompt: str,
        discovery: dict[str, object],
    ) -> str:
        return await self._client.complete_json(
            model=self._model,
            system_prompt=system_prompt,
            payload=discovery,
            max_output_tokens=self._max_output_tokens,
        )


__all__ = ("StructuredDemandIntelligenceModelPort",)
