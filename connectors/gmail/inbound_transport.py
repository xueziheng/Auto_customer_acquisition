"""正文专用typed HTTP端口；不改变feedback的返回/错误契约。"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, build_opener

from pydantic import Field

from connectors.gmail.transport import (
    GmailApiHttpTransport,
    GmailHttpStatusError,
    GmailMalformedResponse,
    GmailNetworkError,
    GmailResponseTooLarge,
    _NoRedirectHandler,
)
from shared.schemas.email_inbound import MIME_BYTES, InboundDTO, InboundError


class GmailInboundRawResult(InboundDTO):
    status: Literal["raw", "too_large", "message_gone", "malformed"]
    raw_mime: bytes | None = Field(
        default=None, repr=False, exclude=True, max_length=MIME_BYTES
    )
    labels: tuple[str, ...] = Field(default=(), repr=False, exclude=True)
    internal_date: datetime | None = Field(default=None, repr=False, exclude=True)


class GmailInboundHttpTransport(Protocol):
    async def get_profile_history_id(self, *, token: str) -> str: ...
    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]: ...
    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]: ...
    async def get_inbound_message(
        self, *, token: str, message_ref: str, maximum_bytes: int
    ) -> GmailInboundRawResult: ...


class _InboundNetworkError(GmailNetworkError):
    """只标记HTTP IO失败，用以区分旧transport解析错误。"""


async def _checked_response[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except _InboundNetworkError:
        raise
    except GmailNetworkError:
        raise GmailMalformedResponse() from None


class GmailInboundApiTransport(GmailApiHttpTransport):
    """生产固定Gmail；受控loopback必须显式且有端口，无重定向。"""

    def __init__(
        self,
        base_url: str = "https://gmail.googleapis.com",
        *,
        controlled_loopback: bool = False,
    ) -> None:
        try:
            url = urlparse(base_url)
            allowed = base_url == "https://gmail.googleapis.com" or (
                controlled_loopback
                and url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
                and url.port is not None
                and not url.path
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
            if not allowed:
                raise ValueError()
        except Exception:  # noqa: BLE001 URL错误不可泄露配置
            raise InboundError() from None
        super().__init__(base_url)
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    def _request(
        self,
        method: str,
        url: str,
        token: str,
        body: bytes | None,
        may_have_written: bool,
        max_response_bytes: int = 65536,
        permanent_overflow: bool = True,
    ) -> dict[str, object]:
        del permanent_overflow
        try:
            return super()._request(
                method, url, token, body, may_have_written, max_response_bytes, True
            )
        except GmailNetworkError as error:
            raise _InboundNetworkError(error.may_have_written) from None

    async def get_profile_history_id(self, *, token: str) -> str:
        return await _checked_response(super().get_profile_history_id(token=token))

    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]:
        return await _checked_response(
            super().list_feedback_messages(
                token=token, after_epoch=after_epoch, page_token=page_token
            )
        )

    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]:
        return await _checked_response(
            super().list_feedback_history(
                token=token, start_history_id=start_history_id, page_token=page_token
            )
        )

    async def get_inbound_message(
        self, *, token: str, message_ref: str, maximum_bytes: int
    ) -> GmailInboundRawResult:
        """HTTP最多编码预算+哨兵；完整性错误/永久超限使用固定typed结果。"""
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MIME_BYTES:
            raise InboundError()
        try:
            payload = await asyncio.to_thread(
                self._request,
                "GET",
                f"{self._base_url}/gmail/v1/users/me/messages/{quote(message_ref, safe='')}?format=raw",
                token,
                None,
                False,
                ((maximum_bytes + 2) // 3 * 4) + 65536,
                True,
            )
        except GmailMalformedResponse:
            return GmailInboundRawResult(status="malformed")
        except GmailResponseTooLarge:
            return GmailInboundRawResult(status="too_large")
        except GmailHttpStatusError as error:
            if error.status_code == 404:
                return GmailInboundRawResult(status="message_gone")
            raise
        try:
            value, labels, stamp = (
                payload.get("raw"),
                payload.get("labelIds"),
                payload.get("internalDate"),
            )
            if (
                not isinstance(value, str)
                or not isinstance(labels, list)
                or len(labels) > 100
                or any(not isinstance(v, str) or len(v) > 200 for v in labels)
                or not isinstance(stamp, str)
                or not stamp.isascii()
                or not stamp.isdigit()
                or len(stamp) > 16
            ):
                raise ValueError()
            if len(value) > (maximum_bytes + 2) // 3 * 4:
                return GmailInboundRawResult(status="too_large")
            decoded = bytearray()
            for start in range(0, len(value), 8192):
                chunk = value[start : start + 8192]
                if start + 8192 >= len(value):
                    chunk += "=" * (-len(chunk) % 4)
                piece = base64.b64decode(chunk, altchars=b"-_", validate=True)
                if len(decoded) + len(piece) > maximum_bytes:
                    return GmailInboundRawResult(status="too_large")
                decoded.extend(piece)
            if not decoded:
                raise ValueError()
            date = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(stamp))
            return GmailInboundRawResult(
                status="raw",
                raw_mime=bytes(decoded),
                labels=tuple(labels),
                internal_date=date,
            )
        except Exception:  # noqa: BLE001 损坏原文永不回显
            return GmailInboundRawResult(status="malformed")
