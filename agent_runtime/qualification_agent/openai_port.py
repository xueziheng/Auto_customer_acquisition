"""回复分类能力到统一结构化模型客户端的生产适配器。"""

from __future__ import annotations

from agent_runtime.model_client import StructuredJsonModelClient
from shared.errors import ValidationError


class StructuredReplyModelPort:
    """只转发已过护栏的 subject/body，并限制模型输出体积。"""

    def __init__(
        self,
        client: StructuredJsonModelClient,
        model: str,
        *,
        max_output_tokens: int = 2_048,
        max_output_bytes: int = 65_536,
    ) -> None:
        if (
            not isinstance(client, StructuredJsonModelClient)
            or not isinstance(model, str)
            or not model
            or model != model.strip()
            or type(max_output_tokens) is not int
            or not 128 <= max_output_tokens <= 8_192
            or type(max_output_bytes) is not int
            or not 1_024 <= max_output_bytes <= 65_536
        ):
            raise ValidationError("回复分类模型配置无效")
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_output_bytes = max_output_bytes

    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        if (
            not isinstance(system_prompt, str)
            or not system_prompt.strip()
            or not isinstance(message, dict)
            or set(message) != {"subject", "body"}
            or any(
                not isinstance(value, str) or not value.strip()
                for value in message.values()
            )
        ):
            raise ValidationError("回复分类模型输入无效")
        output = await self._client.complete_json(
            model=self._model,
            system_prompt=system_prompt,
            payload={"subject": message["subject"], "body": message["body"]},
            max_output_tokens=self._max_output_tokens,
        )
        if not isinstance(output, str):
            raise ValidationError("回复分类模型输出无效")
        if len(output.encode("utf-8")) > self._max_output_bytes:
            raise ValidationError("回复分类模型输出过大")
        return output


__all__ = ("StructuredReplyModelPort",)
