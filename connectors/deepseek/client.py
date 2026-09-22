"""固定 DeepSeek Responses 协议；SDK 仅在网关授权之后惰性构造。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Literal, Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
)
from pydantic import ValidationError

from shared.schemas.model_invocation import ModelRequest, ModelResponse, ModelUsage

FailureCode = Literal[
    "authentication",
    "insufficient_balance",
    "invalid_request",
    "rate_limit",
    "provider_error",
    "invalid_response",
    "unknown",
]
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ModelSecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str:
        """仅在 Connector 内把受信逻辑引用解析成密钥。"""
        ...


class DeepSeekFailure(RuntimeError):
    """固定分类，不携带 Provider 原文与 SDK 异常。"""

    def __init__(
        self,
        code: FailureCode,
        dispatched: bool = True,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__("模型服务调用失败")
        self.code = code
        self.dispatched = dispatched
        self.retry_after_seconds = retry_after_seconds


def _invalid() -> DeepSeekFailure:
    return DeepSeekFailure("invalid_response")


def decode_response(body: Mapping[str, object], expected_model: str) -> ModelResponse:
    """验证完整最终 JSON；缺失 usage 保留未知，拒绝隐式工具调用。"""
    if body.get("status") != "completed" or body.get("model") != expected_model:
        raise _invalid()
    output = body.get("output")
    if not isinstance(output, list):
        raise _invalid()
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            raise _invalid()
        if item.get("type") == "reasoning":
            continue
        if (
            item.get("type") != "message"
            or item.get("role") != "assistant"
            or item.get("status") != "completed"
        ):
            raise _invalid()
        content = item.get("content")
        if not isinstance(content, list):
            raise _invalid()
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise _invalid()
            text = part.get("text")
            if not isinstance(text, str):
                raise _invalid()
            texts.append(text)
    final = "".join(texts)
    if not final or len(final.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise _invalid()
    try:
        value = json.loads(final, parse_constant=lambda _: None)
        if not isinstance(value, dict):
            raise _invalid()
        # 拒绝 JSON 标准外的 NaN/Infinity，即使 Python parser 默认接受。
        json.dumps(json.loads(final), allow_nan=False)
        raw_usage = body.get("usage")
        if raw_usage is not None and not isinstance(raw_usage, dict):
            raise _invalid()
        raw_usage = raw_usage or {}
        details = raw_usage.get("input_tokens_details")
        if details is not None and not isinstance(details, dict):
            raise _invalid()
        usage = ModelUsage(
            input_tokens=raw_usage.get("input_tokens"),
            cached_input_tokens=(details or {}).get("cached_tokens"),
            output_tokens=raw_usage.get("output_tokens"),
        )
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise _invalid() from None
    return ModelResponse(text=final, model=expected_model, usage=usage)


def make_sdk(key: str, timeout_seconds: int) -> AsyncOpenAI:
    """不接受外部 endpoint，避免把凭证转发到不受信地址。"""
    return AsyncOpenAI(
        api_key=key,
        base_url="https://api.deepseek.com",
        max_retries=0,
        timeout=timeout_seconds,
        http_client=DefaultAsyncHttpxClient(
            follow_redirects=False, timeout=timeout_seconds
        ),
    )


class DeepSeekClient:
    def __init__(
        self,
        secret_ref: str,
        resolver: ModelSecretResolver,
        *,
        timeout_seconds: int,
        client_factory: Callable[[str, int], AsyncOpenAI] | None = None,
    ) -> None:
        if (
            not secret_ref
            or secret_ref != secret_ref.strip()
            or type(timeout_seconds) is not int
            or timeout_seconds <= 0
        ):
            raise DeepSeekFailure("invalid_request", dispatched=False)
        self._secret_ref = secret_ref
        self._resolver = resolver
        self._timeout = timeout_seconds
        self._factory = client_factory or make_sdk
        self._client: AsyncOpenAI | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def _sdk(self) -> AsyncOpenAI:
        async with self._lock:
            if self._closed:
                raise DeepSeekFailure("invalid_request", dispatched=False)
            if self._client is None:
                try:
                    key = self._resolver.resolve(self._secret_ref)
                    if not isinstance(key, str) or not key.strip():
                        raise ValueError
                    self._client = self._factory(key, self._timeout)
                except Exception:  # noqa: BLE001 - 不向上层泄漏凭证或 Provider 异常原文
                    raise DeepSeekFailure("authentication", dispatched=False) from None
            return self._client

    async def prepare(self) -> None:
        """只解析凭证并构造 SDK，不发出请求，失败不消耗模型调用。"""
        await self._sdk()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """单次调用，不自行重试；所有返回均经本地完成状态与 JSON 校验。"""
        sdk = await self._sdk()
        try:
            response = await sdk.responses.create(
                model=request.model,
                instructions=request.system_prompt,
                input=json.dumps(request.payload, ensure_ascii=False, allow_nan=False),
                max_output_tokens=request.max_output_tokens,
                store=False,
                text={"format": {"type": "json_object"}},
                reasoning={"effort": "none"},
            )
            return decode_response(response.model_dump(), request.model)
        except DeepSeekFailure:
            raise
        except APIStatusError as exc:
            codes: dict[int, FailureCode] = {
                401: "authentication",
                403: "authentication",
                402: "insufficient_balance",
                429: "rate_limit",
            }
            code: FailureCode = codes.get(
                exc.status_code,
                "provider_error"
                if exc.status_code >= 500
                else "invalid_response"
                if exc.status_code < 400
                else "invalid_request",
            )
            retry = exc.response.headers.get("retry-after", "")
            seconds = (
                int(retry)
                if retry.isascii() and retry.isdigit() and len(retry) < 6
                else None
            )
            if seconds is not None and not 1 <= seconds <= 86400:
                seconds = None
            raise DeepSeekFailure(code, retry_after_seconds=seconds) from None
        except APIConnectionError:
            raise DeepSeekFailure("unknown") from None
        except (TypeError, ValueError, AttributeError):
            raise DeepSeekFailure("invalid_response") from None
        except Exception:  # noqa: BLE001 - 不向上层泄漏凭证或 Provider 异常原文
            raise DeepSeekFailure("unknown") from None

    async def aclose(self) -> None:
        """资源由进程所有者关闭，不触发惰性凭证读取。"""
        async with self._lock:
            self._closed = True
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:  # noqa: BLE001 - 不向上层泄漏凭证或 Provider 异常原文
                    raise DeepSeekFailure("unknown", dispatched=False) from None
                self._client = None
