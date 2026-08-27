"""Tavily 固定 origin 的 HTTP transport；凭证仅作为请求头在本模块使用。"""

from __future__ import annotations

import asyncio
import http.client
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from shared.errors import ConnectorError, RateLimited, TransientError, ValidationError

_TAVILY_HOST = "api.tavily.com"
_SEARCH_PATH = "/search"
_USAGE_PATH = "/usage"
_MAX_RESPONSE_BYTES = 1_048_576


class TavilyConnectorError(ConnectorError):
    """不携带 provider 原始请求、响应或凭证的 Tavily 错误。"""


class TavilyAuthRequiredError(TavilyConnectorError):
    def __init__(self) -> None:
        super().__init__("Tavily 凭证需要恢复")


class TavilyProviderError(TavilyConnectorError):
    def __init__(self) -> None:
        super().__init__("Tavily 拒绝请求")


class TavilyTransientError(TransientError):
    def __init__(self) -> None:
        super().__init__("Tavily 服务暂时不可用")


class TavilyRateLimitedError(RateLimited):
    def __init__(self, retry_after_seconds: int | None = None) -> None:
        if retry_after_seconds is not None and not 1 <= retry_after_seconds <= 3_600:
            raise ValidationError("Tavily retry-after 无效")
        super().__init__("Tavily 调用受限")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class TavilyHttpResponse:
    """已受响应体上限保护的 JSON payload；payload 不参与 repr。"""

    status_code: int
    payload: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.status_code) is not int
            or not 100 <= self.status_code <= 599
            or not isinstance(self.payload, Mapping)
        ):
            raise ValidationError("Tavily 响应无效")


@runtime_checkable
class TavilySearchTransport(Protocol):
    """固定 origin 的 transport，不接受 URL、headers 或可选付费参数。"""

    async def search(
        self, query: str, country: str, limit: int, *, api_key: str
    ) -> TavilyHttpResponse: ...

    async def usage(self, *, api_key: str) -> TavilyHttpResponse: ...


class TavilySearchApiTransport:
    """只向 Tavily `/search`、`/usage` 发送最小固定免费能力请求。"""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValidationError("Tavily timeout 无效")
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return "TavilySearchApiTransport()"

    async def search(
        self, query: str, country: str, limit: int, *, api_key: str
    ) -> TavilyHttpResponse:
        _validate_request(query, country, limit, api_key)
        return await asyncio.to_thread(self._search_sync, query, country, limit, api_key)

    async def usage(self, *, api_key: str) -> TavilyHttpResponse:
        _validate_api_key(api_key)
        return await asyncio.to_thread(self._usage_sync, api_key)

    def _search_sync(
        self, query: str, country: str, limit: int, api_key: str
    ) -> TavilyHttpResponse:
        del country  # 未验证 Tavily ISO country 映射；不改写老板的原查询。
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": limit,
            "topic": "general",
            "auto_parameters": False,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_image_descriptions": False,
            "include_favicon": False,
            "include_usage": False,
        }
        return self._request_sync("POST", _SEARCH_PATH, body, api_key)

    def _usage_sync(self, api_key: str) -> TavilyHttpResponse:
        return self._request_sync("GET", _USAGE_PATH, None, api_key)

    def _request_sync(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
        api_key: str,
    ) -> TavilyHttpResponse:
        connection = http.client.HTTPSConnection(
            _TAVILY_HOST,
            443,
            timeout=self._timeout_seconds,
            context=ssl.create_default_context(),
        )
        raw: bytes | None = None
        status: int | None = None
        retry_after: int | None = None
        try:
            encoded_body = None if body is None else json.dumps(body).encode("utf-8")
            headers = {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "TradeOS-Agent/1.0",
            }
            if encoded_body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=encoded_body, headers=headers)
            response = connection.getresponse()
            status = response.status
            retry_after = _retry_after(response.getheader("Retry-After"))
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise TavilyTransientError() from None
        finally:
            connection.close()
        if status in {401, 403}:
            raise TavilyAuthRequiredError()
        if status == 429:
            raise TavilyRateLimitedError(retry_after)
        if status is None or status >= 500:
            raise TavilyTransientError()
        if status != 200:
            raise TavilyProviderError()
        if raw is None or len(raw) > _MAX_RESPONSE_BYTES:
            raise TavilyTransientError()
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise TavilyTransientError() from None
        if not isinstance(payload, dict):
            raise TavilyTransientError()
        return TavilyHttpResponse(status, payload)


def _validate_request(query: str, country: str, limit: int, api_key: str) -> None:
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= 400
        or len(query.split()) > 50
        or query != query.strip()
        or _has_control(query)
        or not isinstance(country, str)
        or len(country) != 2
        or not country.isascii()
        or not country.isalpha()
        or country != country.upper()
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise ValidationError("Tavily 搜索请求无效")
    _validate_api_key(api_key)


def _validate_api_key(api_key: str) -> None:
    if (
        not isinstance(api_key, str)
        or not api_key
        or api_key != api_key.strip()
        or _has_control(api_key)
    ):
        raise ValidationError("Tavily 凭证无效")


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value, 10)
    except ValueError:
        return None
    return parsed if 1 <= parsed <= 3_600 else None


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


__all__ = (
    "TavilyAuthRequiredError",
    "TavilyConnectorError",
    "TavilyHttpResponse",
    "TavilyProviderError",
    "TavilyRateLimitedError",
    "TavilySearchApiTransport",
    "TavilySearchTransport",
    "TavilyTransientError",
)
