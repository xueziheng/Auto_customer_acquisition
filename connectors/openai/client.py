"""OpenAI Responses API 的惰性、脱敏结构化 JSON 适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from shared.errors import TransientError, ValidationError


@runtime_checkable
class ModelSecretResolver(Protocol):
    """只按逻辑引用解析模型凭证；调用方不得传入明文密钥。"""

    def resolve(self, secret_ref: str) -> str: ...


class OpenAIJsonModelClient:
    """结构化 JSON 模型客户端；不向业务层暴露 SDK 或 Provider 元数据。"""

    def __init__(
        self,
        secret_ref: str,
        resolver: ModelSecretResolver,
        *,
        timeout_seconds: float = 45.0,
        client_factory: Callable[[str, float], AsyncOpenAI] | None = None,
    ) -> None:
        if (
            not isinstance(secret_ref, str)
            or not secret_ref
            or secret_ref != secret_ref.strip()
            or not isinstance(resolver, ModelSecretResolver)
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 120
            or (client_factory is not None and not callable(client_factory))
        ):
            raise ValidationError("模型客户端配置无效")
        self._secret_ref = secret_ref
        self._resolver = resolver
        self._timeout_seconds = float(timeout_seconds)
        self._client_factory = client_factory or self._default_client
        self._client: AsyncOpenAI | None = None
        self._client_lock = asyncio.Lock()

    @staticmethod
    def _default_client(api_key: str, timeout_seconds: float) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    api_key = self._resolver.resolve(self._secret_ref)
                    if not isinstance(api_key, str) or len(api_key) < 20:
                        raise ValidationError("模型凭证配置无效")
                    self._client = self._client_factory(
                        api_key, self._timeout_seconds
                    )
        return self._client

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        if (
            not isinstance(model, str)
            or not model
            or model != model.strip()
            or not isinstance(system_prompt, str)
            or not system_prompt
            or system_prompt != system_prompt.strip()
            or not isinstance(payload, Mapping)
            or type(max_output_tokens) is not int
            or not 64 <= max_output_tokens <= 32_768
        ):
            raise ValidationError("模型调用参数无效")
        try:
            request_body = json.dumps(
                dict(payload), ensure_ascii=False, sort_keys=True
            )
        except (TypeError, ValueError):
            raise ValidationError("模型输入不可序列化") from None
        client = await self._get_client()
        try:
            response = await client.responses.create(
                model=model,
                instructions=system_prompt,
                input=request_body,
                max_output_tokens=max_output_tokens,
                store=False,
                text={"format": {"type": "json_object"}},
            )
        except (APIConnectionError, APITimeoutError, RateLimitError):
            raise TransientError("模型服务暂时不可用") from None
        except APIStatusError as error:
            if error.status_code >= 500:
                raise TransientError("模型服务暂时不可用") from None
            raise ValidationError("模型服务拒绝了请求") from None
        output = response.output_text
        if not isinstance(output, str) or not output:
            raise ValidationError("模型服务未返回结构化内容")
        return output


__all__ = ("ModelSecretResolver", "OpenAIJsonModelClient")
