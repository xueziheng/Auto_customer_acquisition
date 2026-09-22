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
_MAX_RAW_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_RAW_RESPONSE_BYTES = ((_MAX_RAW_MESSAGE_BYTES + 2) // 3 * 4) + 4_096


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


@runtime_checkable
class GmailFeedbackHttpTransport(Protocol):
    async def get_profile_history_id(self, *, token: str) -> str: ...

    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]: ...

    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]: ...

    async def get_raw_message(self, *, token: str, message_ref: str) -> bytes: ...


@dataclass(frozen=True)
class GmailHttpStatusError(Exception):
    """不携带响应 body/header 的 HTTP 失败。"""

    status_code: int
    retry_after_seconds: int | None = None
    may_have_written: bool = False
    feedback_retry_after_seconds: int | None = None
    transactional_retry_after_seconds: int | None = None

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
        if self.feedback_retry_after_seconds is not None and (
            not isinstance(self.feedback_retry_after_seconds, int)
            or isinstance(self.feedback_retry_after_seconds, bool)
            or not 1 <= self.feedback_retry_after_seconds <= 3_600
        ):
            raise ValidationError("Gmail feedback retry-after 无效")
        if self.transactional_retry_after_seconds is not None and (
            not isinstance(self.transactional_retry_after_seconds, int)
            or isinstance(self.transactional_retry_after_seconds, bool)
            or not 1 <= self.transactional_retry_after_seconds <= 3_600
        ):
            raise ValidationError("Gmail transactional retry-after 无效")
        Exception.__init__(self, "Gmail HTTP 调用失败")


@dataclass(frozen=True)
class GmailNetworkError(Exception):
    """不携带底层 socket/URL/凭证信息的网络失败。"""

    may_have_written: bool

    def __post_init__(self) -> None:
        if not isinstance(self.may_have_written, bool):
            raise ValidationError("Gmail 写入状态无效")
        Exception.__init__(self, "Gmail 网络调用失败")


class GmailMalformedResponse(Exception):
    """正文专用永久协议错误，无原始响应。"""


class GmailResponseTooLarge(Exception):
    """正文专用永久预算失败，不携带响应。"""


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
            or not 0 < timeout_seconds <= 30
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

    async def get_profile_history_id(self, *, token: str) -> str:
        payload = await asyncio.to_thread(
            self._request,
            "GET",
            f"{self._base_url}/gmail/v1/users/me/profile",
            token,
            None,
            False,
        )
        return _require_provider_value(payload.get("historyId"), "history id")

    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]:
        parameters: list[tuple[str, str]] = [
            ("q", f"after:{after_epoch}"),
            ("maxResults", "100"),
        ]
        if page_token is not None:
            parameters.append(("pageToken", page_token))
        payload = await asyncio.to_thread(
            self._request,
            "GET",
            f"{self._base_url}/gmail/v1/users/me/messages?{urlencode(parameters)}",
            token,
            None,
            False,
        )
        messages = payload.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 100:
            raise GmailNetworkError(may_have_written=False)
        refs: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                raise GmailNetworkError(may_have_written=False)
            refs.append(_require_provider_value(item.get("id"), "message ref"))
        return tuple(refs), _optional_provider_value(payload.get("nextPageToken"))

    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]:
        parameters: list[tuple[str, str]] = [
            ("startHistoryId", start_history_id),
            ("historyTypes", "messageAdded"),
            ("maxResults", "100"),
        ]
        if page_token is not None:
            parameters.append(("pageToken", page_token))
        payload = await asyncio.to_thread(
            self._request,
            "GET",
            f"{self._base_url}/gmail/v1/users/me/history?{urlencode(parameters)}",
            token,
            None,
            False,
        )
        history = payload.get("history", [])
        if not isinstance(history, list) or len(history) > 100:
            raise GmailNetworkError(may_have_written=False)
        refs: list[str] = []
        seen_refs: set[str] = set()
        for entry in history:
            if not isinstance(entry, dict):
                raise GmailNetworkError(may_have_written=False)
            added = entry.get("messagesAdded", [])
            if not isinstance(added, list):
                raise GmailNetworkError(may_have_written=False)
            for item in added:
                if not isinstance(item, dict) or not isinstance(item.get("message"), dict):
                    raise GmailNetworkError(may_have_written=False)
                message_ref = _require_provider_value(
                    item["message"].get("id"), "message ref"
                )
                if message_ref not in seen_refs:
                    seen_refs.add(message_ref)
                    refs.append(message_ref)
                if len(seen_refs) > 100:
                    raise GmailNetworkError(may_have_written=False)
        return (
            tuple(refs),
            _optional_provider_value(payload.get("nextPageToken")),
            _require_provider_value(payload.get("historyId"), "history id"),
        )

    async def get_raw_message(self, *, token: str, message_ref: str) -> bytes:
        payload = await asyncio.to_thread(
            self._request,
            "GET",
            (
                f"{self._base_url}/gmail/v1/users/me/messages/"
                f"{quote(message_ref, safe='')}?format=raw"
            ),
            token,
            None,
            False,
            _MAX_RAW_RESPONSE_BYTES,
        )
        value = payload.get("raw")
        if not isinstance(value, str) or len(value) > (_MAX_RAW_MESSAGE_BYTES * 2):
            raise GmailNetworkError(may_have_written=False)
        try:
            padding = "=" * (-len(value) % 4)
            raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        except (ValueError, TypeError):
            raise GmailNetworkError(may_have_written=False) from None
        if len(raw) > _MAX_RAW_MESSAGE_BYTES:
            raise GmailNetworkError(may_have_written=False)
        return raw

    def _request(
        self,
        method: str,
        url: str,
        token: str,
        body: bytes | None,
        may_have_written: bool,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        permanent_overflow: bool = False,
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
                length = response.headers.get("Content-Length")
                if permanent_overflow and length is not None:
                    if len(length) > 20 or not length.isascii() or not length.isdigit():
                        raise GmailMalformedResponse()
                    if int(length) > max_response_bytes:
                        raise GmailResponseTooLarge()
                raw = response.read(max_response_bytes + 1)
        except HTTPError as error:
            status = error.code
            retry_after_header = error.headers.get("Retry-After")
            retry_after = _bounded_retry_after(retry_after_header)
            feedback_retry_after = _feedback_retry_after(retry_after_header)
            error.close()
            if not 400 <= status <= 599:
                raise GmailNetworkError(may_have_written=may_have_written) from None
            raise GmailHttpStatusError(
                status,
                retry_after_seconds=retry_after,
                may_have_written=may_have_written,
                feedback_retry_after_seconds=feedback_retry_after,
                transactional_retry_after_seconds=feedback_retry_after,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise GmailNetworkError(may_have_written=may_have_written) from None
        if len(raw) > max_response_bytes:
            if permanent_overflow:
                raise GmailResponseTooLarge()
            raise GmailNetworkError(may_have_written=may_have_written)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            if permanent_overflow:
                raise GmailMalformedResponse() from None
            raise GmailNetworkError(may_have_written=may_have_written) from None
        if not isinstance(payload, dict):
            if permanent_overflow:
                raise GmailMalformedResponse()
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


def _feedback_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value, 10)
    except ValueError:
        return None
    return parsed if 1 <= parsed <= 3_600 else None


def _require_provider_value(value: object, _field: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise GmailNetworkError(may_have_written=False)
    return value


def _optional_provider_value(value: object) -> str | None:
    if value is None:
        return None
    return _require_provider_value(value, "page token")


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
