"""配置化模型 API；沿用既有固定错误和 JSON 输出边界。"""
import base64
import json
from collections.abc import Callable

from openai import APIStatusError, AsyncOpenAI, DefaultAsyncHttpxClient

from connectors.deepseek.client import (
    DeepSeekClient,
    DeepSeekFailure,
    ModelSecretResolver,
    decode_response,
)
from connectors.deepseek.sdk_logging import private_sdk_logs
from shared.schemas.model_connection import MODEL_ENDPOINTS
from shared.schemas.model_invocation import ModelRequest, ModelResponse


class CompatibleModelClient(DeepSeekClient):
    def __init__(self, secret_ref: str, resolver: ModelSecretResolver, *, base_url: str, protocol: str, timeout_seconds: int, client_factory: Callable[[str, int], AsyncOpenAI] | None = None) -> None:
        if base_url not in MODEL_ENDPOINTS or protocol not in ("responses", "chat_completions"):
            raise DeepSeekFailure("invalid_request", dispatched=False)
        self._protocol = protocol
        self._deepseek = base_url.startswith("https://api.deepseek.com")
        super().__init__(secret_ref, resolver, timeout_seconds=timeout_seconds,
            client_factory=client_factory or (lambda key, timeout: AsyncOpenAI(
                api_key=key, base_url=base_url, max_retries=0, timeout=timeout,
                http_client=DefaultAsyncHttpxClient(follow_redirects=False, timeout=timeout, trust_env=False))))

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self._protocol == "responses" and self._deepseek:
            return await super().generate(request)
        sdk = await self._sdk()
        payload = json.dumps(request.payload, ensure_ascii=False, allow_nan=False)
        try:
            with private_sdk_logs():
                if self._protocol == "responses":
                    content = [{"type": "input_text", "text": payload}]
                    for image in request.images:
                        content.append({"type": "input_image", "image_url": f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"})
                    result = await sdk.responses.create(model=request.model, instructions=request.system_prompt,
                        input=[{"role": "user", "content": content}], max_output_tokens=request.max_output_tokens,
                        store=False, text={"format": {"type": "json_object"}})
                    body = result.model_dump()
                    if not isinstance(body.get("model"), str) or not body["model"]:
                        raise DeepSeekFailure("invalid_response")
                    # 供应商允许别名解析为版本模型；账本记录请求的部署模型名。
                    body["model"] = request.model
                    return decode_response(body, request.model)
                content = [{"type": "text", "text": payload}]
                for image in request.images:
                    content.append({"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"}})
                result = await sdk.chat.completions.create(model=request.model,
                    messages=[{"role": "system", "content": request.system_prompt}, {"role": "user", "content": content}],
                    max_tokens=request.max_output_tokens, response_format={"type": "json_object"}, stream=False)
            body = result.model_dump()
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise DeepSeekFailure("invalid_response")
            choice = choices[0]
            if choice.get("finish_reason") == "length":
                raise DeepSeekFailure("output_limit")
            message = choice.get("message", {})
            if choice.get("finish_reason") != "stop" or message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
                raise DeepSeekFailure("invalid_response")
            usage = body.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            return decode_response({"model": request.model, "status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": message.get("content")}]}], "usage": {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"), "input_tokens_details": {"cached_tokens": details.get("cached_tokens")}}}, request.model)
        except DeepSeekFailure:
            raise
        except APIStatusError as exc:
            code = {401: "authentication", 403: "authentication", 402: "insufficient_balance", 429: "rate_limit"}.get(exc.status_code, "provider_error" if exc.status_code >= 500 else "invalid_request")
            raise DeepSeekFailure(code) from None
        except Exception:  # noqa: BLE001 -- 只返回固定安全错误，禁止凭证和 SDK 原文泄漏
            raise DeepSeekFailure("unknown") from None
