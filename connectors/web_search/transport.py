"""Brave 固定主机搜索与防 SSRF 的公开页面 HTTP 传输。"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import SplitResult, urlencode, urljoin, urlsplit, urlunsplit

from shared.errors import TransientError, ValidationError

_BRAVE_HOST = "api.search.brave.com"
_BRAVE_PATH = "/res/v1/web/search"
_MAX_SEARCH_BYTES = 1_048_576
_MAX_PAGE_BYTES = 2_097_152
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class WebSearchAuthRequiredError(ValidationError):
    def __init__(self) -> None:
        super().__init__("公开搜索凭证需要恢复")


class WebSearchRateLimitedError(TransientError):
    def __init__(self, retry_after_seconds: int | None = None) -> None:
        if retry_after_seconds is not None and not 1 <= retry_after_seconds <= 3_600:
            raise ValidationError("公开搜索重试时间无效")
        super().__init__("公开搜索调用受限")
        self.retry_after_seconds = retry_after_seconds


class WebSearchProviderError(ValidationError):
    def __init__(self) -> None:
        super().__init__("公开搜索服务拒绝请求")


class PublicPageRejectedError(ValidationError):
    def __init__(self) -> None:
        super().__init__("公开页面地址或响应不允许")


@dataclass(frozen=True)
class SearchHttpResponse:
    """Brave 成功响应；Provider payload 不参与 repr。"""

    payload: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise ValidationError("公开搜索响应无效")


@dataclass(frozen=True, repr=False)
class PublicPageResponse:
    """完成全部网络边界检查后的 HTML bytes。"""

    url: str
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.url, str)
            or not self.url
            or not isinstance(self.body, bytes)
            or not self.body
            or len(self.body) > _MAX_PAGE_BYTES
        ):
            raise ValidationError("公开页面响应无效")


@runtime_checkable
class BraveSearchTransport(Protocol):
    async def search(
        self,
        query: str,
        country: str,
        count: int,
        *,
        api_key: str,
    ) -> SearchHttpResponse: ...


@runtime_checkable
class PublicPageTransport(Protocol):
    async def validate_url(self, url: str) -> str: ...

    async def fetch(self, url: str) -> PublicPageResponse: ...


class BraveSearchApiTransport:
    """只访问 Brave Search API 固定 host/path，不接受调用方 URL。"""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValidationError("公开搜索 timeout 无效")
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return "BraveSearchApiTransport()"

    async def search(
        self,
        query: str,
        country: str,
        count: int,
        *,
        api_key: str,
    ) -> SearchHttpResponse:
        _validate_search_request(query, country, count, api_key)
        return await asyncio.to_thread(
            self._search_sync, query, country, count, api_key
        )

    def _search_sync(
        self, query: str, country: str, count: int, api_key: str
    ) -> SearchHttpResponse:
        connection = http.client.HTTPSConnection(
            _BRAVE_HOST,
            443,
            timeout=self._timeout_seconds,
            context=ssl.create_default_context(),
        )
        raw: bytes | None = None
        status: int | None = None
        retry_after: int | None = None
        try:
            params = urlencode(
                {
                    "q": query,
                    "country": country,
                    "count": count,
                    "safesearch": "strict",
                }
            )
            connection.request(
                "GET",
                f"{_BRAVE_PATH}?{params}",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-Subscription-Token": api_key,
                    "User-Agent": "TradeOS-Agent/1.0",
                },
            )
            response = connection.getresponse()
            status = response.status
            retry_after = _retry_after(response.getheader("Retry-After"))
            raw = response.read(_MAX_SEARCH_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise TransientError("公开搜索服务暂时不可用") from None
        finally:
            connection.close()
        if status in {401, 403}:
            raise WebSearchAuthRequiredError()
        if status == 429:
            raise WebSearchRateLimitedError(retry_after)
        if status != 200:
            if status is not None and status >= 500:
                raise TransientError("公开搜索服务暂时不可用")
            raise WebSearchProviderError()
        if raw is None or len(raw) > _MAX_SEARCH_BYTES:
            raise TransientError("公开搜索服务暂时不可用")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise TransientError("公开搜索服务暂时不可用") from None
        if not isinstance(payload, dict):
            raise TransientError("公开搜索服务暂时不可用")
        return SearchHttpResponse(payload)


class SafePublicPageHttpTransport:
    """逐跳校验 URL/DNS/实际对端 IP；在发请求前阻断私网地址。"""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValidationError("公开页面 timeout 无效")
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return "SafePublicPageHttpTransport()"

    async def validate_url(self, url: str) -> str:
        return await asyncio.to_thread(_validate_public_url_sync, url)

    async def fetch(self, url: str) -> PublicPageResponse:
        canonical = await self.validate_url(url)
        return await asyncio.to_thread(self._fetch_sync, canonical)

    def _fetch_sync(self, url: str) -> PublicPageResponse:
        current = url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            parsed = _validated_public_split(current)
            response = self._request_once(parsed)
            if response[0] in _REDIRECT_STATUSES:
                if redirect_count == _MAX_REDIRECTS or response[1] is None:
                    raise PublicPageRejectedError()
                current = _validate_public_url_sync(urljoin(current, response[1]))
                continue
            status, _location, content_type, content_encoding, body = response
            if (
                status != 200
                or content_type is None
                or content_type.split(";", 1)[0].strip().casefold() != "text/html"
                or content_encoding not in {None, "", "identity"}
                or body is None
                or not body
                or len(body) > _MAX_PAGE_BYTES
            ):
                if status >= 500:
                    raise TransientError("公开页面暂时不可用")
                raise PublicPageRejectedError()
            return PublicPageResponse(current, body)
        raise PublicPageRejectedError()

    def _request_once(
        self, parsed: SplitResult
    ) -> tuple[int, str | None, str | None, str | None, bytes | None]:
        hostname = parsed.hostname
        if hostname is None:
            raise PublicPageRejectedError()
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                hostname,
                parsed.port,
                timeout=self._timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                hostname,
                parsed.port,
                timeout=self._timeout_seconds,
            )
        try:
            connection.connect()
            sock = connection.sock
            if sock is None or not _is_public_ip(sock.getpeername()[0]):
                raise PublicPageRejectedError()
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "text/html",
                    "Accept-Encoding": "identity",
                    "User-Agent": "TradeOS-Agent/1.0",
                },
            )
            response = connection.getresponse()
            status = response.status
            location = response.getheader("Location")
            content_type = response.getheader("Content-Type")
            content_encoding = response.getheader("Content-Encoding")
            body = None
            if status not in _REDIRECT_STATUSES:
                body = response.read(_MAX_PAGE_BYTES + 1)
            return status, location, content_type, content_encoding, body
        except PublicPageRejectedError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            raise TransientError("公开页面暂时不可用") from None
        finally:
            connection.close()


def _validate_search_request(
    query: str, country: str, count: int, api_key: str
) -> None:
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
        or type(count) is not int
        or not 1 <= count <= 20
        or not isinstance(api_key, str)
        or not api_key
        or api_key != api_key.strip()
        or _has_control(api_key)
    ):
        raise ValidationError("公开搜索请求无效")


def _validate_public_url_sync(url: str) -> str:
    parsed = _validated_public_split(url)
    hostname = parsed.hostname
    if hostname is None:
        raise PublicPageRejectedError()
    try:
        addresses = socket.getaddrinfo(
            hostname,
            parsed.port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError):
        raise PublicPageRejectedError() from None
    resolved = {
        item[4][0] for item in addresses if isinstance(item[4][0], str)
    }
    if not resolved or any(not _is_public_ip(address) for address in resolved):
        raise PublicPageRejectedError()
    return urlunsplit(parsed)


def _validated_public_split(url: str) -> SplitResult:
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 2_048
        or url != url.strip()
        or _has_control(url)
    ):
        raise PublicPageRejectedError()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, UnicodeError):
        raise PublicPageRejectedError() from None
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or not hostname.isascii()
        or hostname != hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.scheme == "http" and port not in {None, 80})
        or (parsed.scheme == "https" and port not in {None, 443})
    ):
        raise PublicPageRejectedError()
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise PublicPageRejectedError()
    canonical_port = None if port in {None, 80, 443} else port
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if canonical_port is not None:
        netloc = f"{netloc}:{canonical_port}"
    return SplitResult(
        parsed.scheme,
        netloc,
        parsed.path or "/",
        parsed.query,
        "",
    )


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


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
    "BraveSearchApiTransport",
    "BraveSearchTransport",
    "PublicPageRejectedError",
    "PublicPageResponse",
    "PublicPageTransport",
    "SafePublicPageHttpTransport",
    "SearchHttpResponse",
    "WebSearchAuthRequiredError",
    "WebSearchProviderError",
    "WebSearchRateLimitedError",
)
