"""固定官方 HTTPS 搜索；无代理、重定向和重试。"""

from __future__ import annotations

import asyncio
import http.client
import json
import ssl
from collections.abc import Mapping
from typing import Protocol

from shared.errors import TransientError, ValidationError


class SearchTransport(Protocol):
    async def search(
        self, query: str, country: str, limit: int, *, api_key: str
    ) -> Mapping[str, object]: ...


class DeepSeekSearchError(TransientError):
    """只保留固定错误分类，调用方不得据此自动重新派发。"""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("搜索未完成：" + category)


class DeepSeekSearchPermanentError(ValidationError):
    """认证、余额、请求拒绝必须修复配置，不是重试许可。"""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("搜索被拒绝：" + category)


def validate_request(query: str, country: str, limit: int) -> None:
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= 128
        or query != query.strip()
        or not query.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in query)
        or not isinstance(country, str)
        or len(country) != 2
        or not country.isascii()
        or not country.isalpha()
        or country != country.upper()
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise ValidationError("搜索公开条件无效")


class DeepSeekSearchApiTransport:
    """一次调用最多一个有界供应商请求，不读取环境代理。"""

    def __init__(self, *, timeout_seconds: int = 10) -> None:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            raise ValidationError("搜索超时配置无效")
        self.timeout_seconds = timeout_seconds

    async def search(
        self, query: str, country: str, limit: int, *, api_key: str
    ) -> Mapping[str, object]:
        validate_request(query, country, limit)
        if not api_key or any(ord(c) < 33 or ord(c) == 127 for c in api_key):
            raise DeepSeekSearchPermanentError("authentication")
        return await asyncio.to_thread(self._request, query, limit, api_key)

    def _request(self, query: str, limit: int, api_key: str) -> Mapping[str, object]:
        connection = http.client.HTTPSConnection(
            "api.deepseek.com",
            443,
            timeout=self.timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(
                "POST",
                "/v1/tools/search",
                body=json.dumps(
                    {"query": query, "count": limit, "need_content": False}
                ).encode(),
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            status = response.status
            raw = response.read(1_048_577)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise DeepSeekSearchError("unknown") from None
        finally:
            connection.close()
        if status in {400, 401, 402, 403, 422}:
            raise DeepSeekSearchPermanentError(
                {401: "authentication", 402: "balance"}.get(status, "request_rejected")
            )
        if status != 200:
            raise DeepSeekSearchError("rate_limit" if status == 429 else "unknown")
        if len(raw) > 1_048_576:
            raise DeepSeekSearchError("unknown")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise DeepSeekSearchError("unknown") from None
        if not isinstance(payload, dict):
            raise DeepSeekSearchError("unknown")
        return payload
