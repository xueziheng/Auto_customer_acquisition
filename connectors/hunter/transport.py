"""Hunter API v2 的固定主机、限资源 HTTP 传输边界。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Self, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shared.errors import ValidationError

_BASE_URL = "https://api.hunter.io/v2"
_ALLOWED_PATHS = frozenset({"/account", "/domain-search", "/email-verifier"})
_MAX_RESPONSE_BYTES = 524_288
_MAX_RETRY_AFTER_SECONDS = 3_600


class _ReadableResponse(Protocol):
    status: int
    headers: Any

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> object: ...


class _Opener(Protocol):
    def open(self, request: Request, *, timeout: float) -> _ReadableResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """把 redirect 保留为 HTTP 失败，防止凭证 header 被带往其他主机。"""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


class HunterErrorCode(str, Enum):
    CLAIMED_EMAIL = "claimed_email"
    OTHER = "other"


@dataclass(frozen=True)
class HunterHttpResponse:
    """成功响应；payload 可读取但不参与 repr。"""

    status_code: int
    payload: Mapping[str, object] = field(repr=False)
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if (
            not _valid_status(self.status_code, minimum=100)
            or not isinstance(self.payload, Mapping)
            or not _valid_retry_after(self.retry_after_seconds)
        ):
            raise ValidationError("Hunter HTTP 响应无效")


@runtime_checkable
class HunterHttpTransport(Protocol):
    async def get(
        self,
        path: str,
        params: tuple[tuple[str, str], ...],
        *,
        api_key: str,
    ) -> HunterHttpResponse:
        """向固定 Hunter endpoint 发起一次有界 GET。"""
        raise NotImplementedError


@dataclass(frozen=True)
class HunterHttpStatusError(Exception):
    """不携带 URL、header 或 response body 的 Hunter HTTP 失败。"""

    status_code: int
    error_code: HunterErrorCode
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if (
            not _valid_status(self.status_code, minimum=400)
            or not isinstance(self.error_code, HunterErrorCode)
            or not _valid_retry_after(self.retry_after_seconds)
        ):
            raise ValidationError("Hunter HTTP 状态无效")
        Exception.__init__(self, "Hunter HTTP 调用失败")


@dataclass(frozen=True)
class HunterNetworkError(Exception):
    """不携带底层异常信息的网络或响应格式失败。"""

    may_have_reached_provider: bool

    def __post_init__(self) -> None:
        if not isinstance(self.may_have_reached_provider, bool):
            raise ValidationError("Hunter 网络状态无效")
        Exception.__init__(self, "Hunter 网络调用失败")


class HunterApiHttpTransport:
    """使用 urllib 访问固定 Hunter API v2 host 的生产传输。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        opener: _Opener | None = None,
    ) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValidationError("Hunter timeout 无效")
        self._timeout_seconds = float(timeout_seconds)
        self._opener: _Opener = opener or build_opener(_NoRedirectHandler())

    def __repr__(self) -> str:
        return "HunterApiHttpTransport()"

    async def get(
        self,
        path: str,
        params: tuple[tuple[str, str], ...],
        *,
        api_key: str,
    ) -> HunterHttpResponse:
        _validate_request(path, params, api_key)
        return await asyncio.to_thread(self._request, path, params, api_key)

    def _request(
        self,
        path: str,
        params: tuple[tuple[str, str], ...],
        api_key: str,
    ) -> HunterHttpResponse:
        query = urlencode(sorted(params))
        url = f"{_BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"
        request = Request(
            url,
            data=None,
            headers={"X-API-KEY": api_key},
            method="GET",
        )

        raw: bytes | None = None
        status: int | None = None
        retry_after: int | None = None
        safe_status_error: HunterHttpStatusError | None = None
        network_failed = False
        try:
            with self._opener.open(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                status = response.status
                retry_after = _parse_retry_after(
                    _safe_header(response.headers, "Retry-After")
                )
        except HTTPError as error:
            try:
                try:
                    error_body = error.read(_MAX_RESPONSE_BYTES + 1)
                    error_code = _parse_error_code(error_body)
                    parsed_retry_after = _parse_retry_after(
                        _safe_header(error.headers, "Retry-After")
                    )
                    if _valid_status(error.code, minimum=400):
                        safe_status_error = HunterHttpStatusError(
                            error.code,
                            error_code,
                            parsed_retry_after,
                        )
                    else:
                        network_failed = True
                except (OSError, ValueError, TypeError):
                    network_failed = True
            finally:
                try:
                    error.close()
                except OSError:
                    network_failed = True
                    safe_status_error = None
        except (URLError, TimeoutError, OSError):
            network_failed = True

        if safe_status_error is not None:
            raise safe_status_error
        if network_failed or raw is None or status is None:
            raise HunterNetworkError(may_have_reached_provider=True)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise HunterNetworkError(may_have_reached_provider=True)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, RecursionError):
            raise HunterNetworkError(may_have_reached_provider=True) from None
        if not isinstance(payload, dict) or not _valid_status(status, minimum=100):
            raise HunterNetworkError(may_have_reached_provider=True)
        return HunterHttpResponse(status, payload, retry_after)


def _validate_request(
    path: str,
    params: tuple[tuple[str, str], ...],
    api_key: str,
) -> None:
    params_valid = type(params) is tuple and all(
        type(item) is tuple
        and len(item) == 2
        and all(
            isinstance(value, str)
            and bool(value)
            and not any(ord(character) < 32 or ord(character) == 127 for character in value)
            for value in item
        )
        and item[0].casefold() not in {"api_key", "authorization", "x-api-key"}
        for item in params
    )
    api_key_valid = (
        isinstance(api_key, str)
        and bool(api_key)
        and api_key == api_key.strip()
        and not any(ord(character) < 33 or ord(character) == 127 for character in api_key)
    )
    if path not in _ALLOWED_PATHS or not params_valid or not api_key_valid:
        raise ValidationError("Hunter 请求参数无效")


def _parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value, 10)
    except ValueError:
        return None
    return parsed if 1 <= parsed <= _MAX_RETRY_AFTER_SECONDS else None


def _parse_error_code(raw: bytes) -> HunterErrorCode:
    if len(raw) > _MAX_RESPONSE_BYTES:
        return HunterErrorCode.OTHER
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, RecursionError):
        return HunterErrorCode.OTHER
    if not isinstance(payload, dict):
        return HunterErrorCode.OTHER
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return HunterErrorCode.OTHER
    for error in errors:
        if isinstance(error, dict) and error.get("id") == "claimed_email":
            return HunterErrorCode.CLAIMED_EMAIL
    return HunterErrorCode.OTHER


def _valid_status(value: object, *, minimum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= 599
    )


def _safe_header(headers: object, name: str) -> str | None:
    try:
        value = headers.get(name)  # type: ignore[attr-defined]
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _valid_retry_after(value: object) -> bool:
    return value is None or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _MAX_RETRY_AFTER_SECONDS
    )
