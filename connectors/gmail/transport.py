"""Gmail API 的最小 HTTP 传输边界。"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shared.errors import ValidationError

_MAX_RESPONSE_BYTES = 65_536


class _NoRedirectHandler(HTTPRedirectHandler):
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


@runtime_checkable
class GmailHttpTransport(Protocol):
    async def search(
        self, *, token: str, message_id: str, header: str
    ) -> str | None: ...

    async def send(self, *, token: str, raw_message: bytes) -> str: ...


@dataclass(frozen=True)
class GmailHttpStatusError(Exception):
    """不携带响应 body/header 的 HTTP 失败。"""

    status_code: int
    retry_after_seconds: int | None = None
    may_have_written: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status_code, int)
            or isinstance(self.status_code, bool)
            or not 400 <= self.status_code <= 599
        ):
            raise ValidationError("Gmail HTTP 状态无效")
        if self.retry_after_seconds is not None and (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or not 1 <= self.retry_after_seconds <= 86_400
        ):
            raise ValidationError("Gmail retry-after 无效")
        if not isinstance(self.may_have_written, bool):
            raise ValidationError("Gmail 写入状态无效")
        Exception.__init__(self, "Gmail HTTP 调用失败")


@dataclass(frozen=True)
class GmailNetworkError(Exception):
    """不携带底层 socket/URL/凭证信息的网络失败。"""

    may_have_written: bool

    def __post_init__(self) -> None:
        if not isinstance(self.may_have_written, bool):
            raise ValidationError("Gmail 写入状态无效")
        Exception.__init__(self, "Gmail 网络调用失败")


class GmailApiHttpTransport:
    """使用标准库访问 Gmail messages.list/send 的受限适配器。"""

    def __init__(
        self,
        base_url: str = "https://gmail.googleapis.com",
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlparse(base_url)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if (
            not isinstance(base_url, str)
            or (parsed.scheme != "https" and not loopback_http)
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValidationError("Gmail base URL 无效")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValidationError("Gmail timeout 无效")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._opener = build_opener(_NoRedirectHandler())

    async def search(
        self, *, token: str, message_id: str, header: str
    ) -> str | None:
        query = urlencode(
            {
                "q": f"rfc822msgid:{message_id}",
                "maxResults": "10",
            }
        )
        payload = await asyncio.to_thread(
            self._request,
            "GET",
            f"{self._base_url}/gmail/v1/users/me/messages?{query}",
            token,
            None,
            False,
        )
        messages = payload.get("messages")
        if messages in (None, []):
            return None
        if not isinstance(messages, list) or not messages:
            raise GmailNetworkError(may_have_written=False)
        for candidate in messages:
            if not isinstance(candidate, dict) or not isinstance(
                candidate.get("id"), str
            ):
                raise GmailNetworkError(may_have_written=False)
            candidate_id = candidate["id"]
            metadata_query = urlencode(
                [
                    ("format", "metadata"),
                    ("metadataHeaders", "Message-ID"),
                    ("metadataHeaders", "X-TradeOS-Idempotency-V1"),
                ]
            )
            metadata = await asyncio.to_thread(
                self._request,
                "GET",
                (
                    f"{self._base_url}/gmail/v1/users/me/messages/"
                    f"{quote(candidate_id, safe='')}?{metadata_query}"
                ),
                token,
                None,
                False,
            )
            if _metadata_matches(metadata, message_id, header):
                return candidate_id
        return None

    async def send(self, *, token: str, raw_message: bytes) -> str:
        body = json.dumps(
            {"raw": base64.urlsafe_b64encode(raw_message).decode("ascii")},
            separators=(",", ":"),
        ).encode("ascii")
        payload = await asyncio.to_thread(
            self._request,
            "POST",
            f"{self._base_url}/gmail/v1/users/me/messages/send",
            token,
            body,
            True,
        )
        provider_ref = payload.get("id")
        if not isinstance(provider_ref, str):
            raise GmailNetworkError(may_have_written=True)
        return provider_ref

    def _request(
        self,
        method: str,
        url: str,
        token: str,
        body: bytes | None,
        may_have_written: bool,
    ) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            retry_after = _bounded_retry_after(error.headers.get("Retry-After"))
            error.close()
            if not 400 <= status <= 599:
                raise GmailNetworkError(may_have_written=may_have_written) from None
            raise GmailHttpStatusError(
                status,
                retry_after_seconds=retry_after,
                may_have_written=may_have_written,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise GmailNetworkError(may_have_written=may_have_written) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise GmailNetworkError(may_have_written=may_have_written)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise GmailNetworkError(may_have_written=may_have_written) from None
        if not isinstance(payload, dict):
            raise GmailNetworkError(may_have_written=may_have_written)
        return payload


def _bounded_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value, 10)
    except ValueError:
        return None
    return min(86_400, max(1, parsed))


def _metadata_matches(
    metadata: dict[str, object], message_id: str, header: str
) -> bool:
    payload = metadata.get("payload")
    if not isinstance(payload, dict):
        return False
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return False
    values: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, dict):
            return False
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return False
        values[name.casefold()] = value
    return (
        values.get("message-id") == f"<{message_id}>"
        and values.get("x-tradeos-idempotency-v1") == header
    )
