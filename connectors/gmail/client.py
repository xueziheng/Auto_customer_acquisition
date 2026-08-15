"""Gmail 单封发送、确定性恢复与 typed 投递反馈读取连接器。"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.policy import SMTP, default
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlparse

from connectors.base import ConnectorManifest
from shared.errors import ValidationError
from shared.schemas.email_feedback import EmailFeedbackItem, EmailFeedbackPage
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError

from .arf import parse_abuse_report
from .feedback import (
    MAX_FEEDBACK_MIME_BYTES,
    message_occurred_at,
    parse_delivery_status,
    provider_ref_digest,
)
from .transport import (
    GmailFeedbackHttpTransport,
    GmailHttpStatusError,
    GmailHttpTransport,
    GmailNetworkError,
)

MANIFEST = ConnectorManifest(
    connector_id="gmail",
    capabilities=(
        "email.send",
        "notification.email.send",
        "email.feedback.fetch",
        "email.add_label",
    ),
    secret_refs=("GMAIL_OAUTH_TOKEN_REF",),
    rate_limit_note="Gmail API 按用户配额；429 带 retry_after",
    compliance_note="发送必须含退订链接；见 docs/architecture/08-compliance.md",
)

_MAILBOX_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
_MESSAGE_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}@[a-z0-9.-]{1,127}"
)
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_SECRET_MARKERS = ("bearer", "secret", "token", "password", "authorization")


@runtime_checkable
class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


@dataclass(frozen=True)
class GmailSendRequest:
    from_address: str = field(repr=False)
    recipient_address: str = field(repr=False)
    subject: str = field(repr=False)
    body: str = field(repr=False)
    unsubscribe_url: str = field(repr=False)
    deterministic_message_id: str
    idempotency_header: str

    def __post_init__(self) -> None:
        _validate_mailbox(self.from_address)
        _validate_mailbox(self.recipient_address)
        _validate_text(self.subject, max_bytes=998, allow_newlines=False)
        _validate_text(self.body, max_bytes=1_048_576, allow_newlines=True)
        _validate_https_url(self.unsubscribe_url)
        if _MESSAGE_ID_RE.fullmatch(self.deterministic_message_id) is None:
            raise ValidationError("Gmail Message-ID 无效")
        _validate_safe_id(self.idempotency_header, "Gmail 幂等 header 无效")


@dataclass(frozen=True)
class GmailTransactionalSendRequest:
    """内部事务通知发送材料；与冷开发合同和退订 header 完全分离。"""

    from_address: str = field(repr=False)
    recipient_address: str = field(repr=False)
    subject: str = field(repr=False)
    body: str = field(repr=False)
    deterministic_message_id: str
    idempotency_header: str

    def __post_init__(self) -> None:
        _validate_mailbox(self.from_address)
        _validate_mailbox(self.recipient_address)
        _validate_text(self.subject, max_bytes=998, allow_newlines=False)
        _validate_text(self.body, max_bytes=1_048_576, allow_newlines=True)
        for value in (
            self.from_address,
            self.recipient_address,
            self.subject,
            self.body,
            self.deterministic_message_id,
            self.idempotency_header,
        ):
            if _credential_shaped(value):
                raise ValidationError("Gmail transactional metadata 无效")
        if _MESSAGE_ID_RE.fullmatch(self.deterministic_message_id) is None:
            raise ValidationError("Gmail Message-ID 无效")
        _validate_safe_id(self.idempotency_header, "Gmail 幂等 header 无效")


@dataclass(frozen=True)
class GmailSendResult:
    provider_ref: str | None
    certainty: DeliveryCertainty
    already_existed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.certainty, DeliveryCertainty):
            raise ValidationError("Gmail 送达确定性无效")
        if not isinstance(self.already_existed, bool):
            raise ValidationError("Gmail existing 标记无效")
        if self.provider_ref is not None:
            _validate_safe_id(self.provider_ref, "Gmail provider ref 无效")
        if self.certainty is DeliveryCertainty.SENT and self.provider_ref is None:
            raise ValidationError("Gmail sent 结果缺少 provider ref")
        if self.certainty is not DeliveryCertainty.SENT and self.provider_ref is not None:
            raise ValidationError("Gmail 未确认结果不能携带 provider ref")
        if self.already_existed and self.certainty is not DeliveryCertainty.SENT:
            raise ValidationError("Gmail existing 结果无效")


@dataclass(frozen=True)
class _SecretValue:
    value: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class _FeedbackCursorState:
    mode: str
    after_epoch: int | None
    boundary_history_id: str | None
    start_history_id: str | None
    page_token: str | None
    pending_message_refs: tuple[str, ...]
    pending_block_offset: int


class GmailConnector:
    manifest = MANIFEST

    def __init__(
        self,
        transport: GmailHttpTransport,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(transport, GmailHttpTransport):
            raise ValidationError("Gmail transport 无效")
        self._transport = transport
        self._token: _SecretValue | None = None
        self._now = now

    async def configure(self, secret_resolver: SecretResolver) -> None:
        if not isinstance(secret_resolver, SecretResolver):
            raise ValidationError("Gmail secret resolver 无效")
        token = secret_resolver.resolve("GMAIL_OAUTH_TOKEN_REF")
        if not isinstance(token, str) or not token or any(ord(ch) < 32 for ch in token):
            raise ValidationError("Gmail 凭证无效")
        self._token = _SecretValue(token)

    async def health_check(self) -> bool:
        return self._token is not None

    async def send_once(self, request: GmailSendRequest) -> GmailSendResult:
        if not isinstance(request, GmailSendRequest):
            raise ValidationError("Gmail send request 无效")
        if self._token is None:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        token = self._token.value
        try:
            existing = await self._transport.search(
                token=token,
                message_id=request.deterministic_message_id,
                header=request.idempotency_header,
            )
        except (GmailHttpStatusError, GmailNetworkError) as error:
            raise _classify_transport_error(error) from None
        if existing is not None:
            return GmailSendResult(existing, DeliveryCertainty.SENT, True)
        try:
            provider_ref = await self._transport.send(
                token=token, raw_message=_build_message(request)
            )
        except (GmailHttpStatusError, GmailNetworkError) as error:
            raise _classify_transport_error(error) from None
        return GmailSendResult(provider_ref, DeliveryCertainty.SENT, False)

    async def reconcile_once(self, request: GmailSendRequest) -> GmailSendResult:
        """只搜索确定性 header；未命中仍保持人工对账，绝不发送。"""
        if not isinstance(request, GmailSendRequest):
            raise ValidationError("Gmail send request 无效")
        if self._token is None:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        try:
            existing = await self._transport.search(
                token=self._token.value,
                message_id=request.deterministic_message_id,
                header=request.idempotency_header,
            )
        except (GmailHttpStatusError, GmailNetworkError) as error:
            raise _classify_transport_error(error) from None
        if existing is None:
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        return GmailSendResult(existing, DeliveryCertainty.SENT, True)

    async def send_transactional_once(
        self, request: GmailTransactionalSendRequest
    ) -> GmailSendResult:
        """按确定性 header 查重后发送内部通知，不生成客户退订 header。"""
        if not isinstance(request, GmailTransactionalSendRequest):
            raise ValidationError("Gmail transactional request 无效")
        if self._token is None:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        token = self._token.value
        try:
            existing = await self._transport.search(
                token=token,
                message_id=request.deterministic_message_id,
                header=request.idempotency_header,
            )
        except (GmailHttpStatusError, GmailNetworkError) as error:
            raise _classify_transactional_transport_error(error) from None
        if existing is not None:
            return GmailSendResult(existing, DeliveryCertainty.SENT, True)
        try:
            provider_ref = await self._transport.send(
                token=token,
                raw_message=_build_transactional_message(request),
            )
        except (GmailHttpStatusError, GmailNetworkError) as error:
            raise _classify_transactional_transport_error(error) from None
        return GmailSendResult(provider_ref, DeliveryCertainty.SENT, False)

    async def reconcile_transactional_once(
        self, request: GmailTransactionalSendRequest
    ) -> GmailSendResult:
        """事务通知的不确定写入恢复只搜索，未命中绝不自动重发。"""
        if not isinstance(request, GmailTransactionalSendRequest):
            raise ValidationError("Gmail transactional request 无效")
        if self._token is None:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        try:
            existing = await self._transport.search(
                token=self._token.value,
                message_id=request.deterministic_message_id,
                header=request.idempotency_header,
            )
        except (GmailHttpStatusError, GmailNetworkError) as error:
            raise _classify_transactional_transport_error(error) from None
        if existing is None:
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        return GmailSendResult(existing, DeliveryCertainty.SENT, True)

    async def send(
        self,
        idempotency_key: str,
        from_identity: str,
        to: str,
        subject: str,
        body: str,
        unsubscribe_url: str,
    ) -> str:
        del idempotency_key, from_identity, to, subject, body, unsubscribe_url
        raise NotImplementedError

    async def fetch_feedback_page(
        self,
        mailbox_alias: str,
        cursor: str | None,
        page_limit: int,
    ) -> EmailFeedbackPage:
        if (
            not isinstance(mailbox_alias, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", mailbox_alias) is None
            or not isinstance(page_limit, int)
            or isinstance(page_limit, bool)
            or not 1 <= page_limit <= 100
        ):
            raise ValidationError("Gmail feedback request 无效")
        if self._token is None:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        if not isinstance(self._transport, GmailFeedbackHttpTransport):
            raise ValidationError("Gmail feedback transport 无效")
        feedback_transport = cast(GmailFeedbackHttpTransport, self._transport)
        starting_cursor = cursor
        try:
            state = (
                _decode_feedback_cursor(cursor)
                if cursor is not None
                else await self._initial_feedback_state(
                    self._token.value, feedback_transport
                )
            )
            refs, continuation = await self._feedback_refs(
                self._token.value, state, feedback_transport
            )
            items: list[EmailFeedbackItem] = []
            pending = list(refs)
            offset = state.pending_block_offset if state.pending_message_refs else 0
            while pending:
                message_ref = pending[0]
                raw = await feedback_transport.get_raw_message(
                    token=self._token.value, message_ref=message_ref
                )
                parsed = _parse_feedback_message(raw, message_ref)
                remaining = parsed[offset:]
                capacity = page_limit - len(items)
                items.extend(remaining[:capacity])
                consumed = min(len(remaining), capacity)
                if consumed < len(remaining):
                    continuation = _FeedbackCursorState(
                        continuation.mode,
                        continuation.after_epoch,
                        continuation.boundary_history_id,
                        continuation.start_history_id,
                        continuation.page_token,
                        tuple(pending),
                        offset + consumed,
                    )
                    break
                pending.pop(0)
                offset = 0
                if len(items) == page_limit and pending:
                    continuation = _FeedbackCursorState(
                        continuation.mode,
                        continuation.after_epoch,
                        continuation.boundary_history_id,
                        continuation.start_history_id,
                        continuation.page_token,
                        tuple(pending),
                        0,
                    )
                    break
            else:
                continuation = _FeedbackCursorState(
                    continuation.mode,
                    continuation.after_epoch,
                    continuation.boundary_history_id,
                    continuation.start_history_id,
                    continuation.page_token,
                    (),
                    0,
                )
        except (GmailHttpStatusError, GmailNetworkError) as error:
            raise _classify_feedback_transport_error(error) from None
        next_cursor = _encode_feedback_cursor(continuation)
        return EmailFeedbackPage(starting_cursor, next_cursor, tuple(items))

    async def _initial_feedback_state(
        self, token: str, transport: GmailFeedbackHttpTransport
    ) -> _FeedbackCursorState:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValidationError("Gmail feedback clock 无效")
        history_id = await transport.get_profile_history_id(token=token)
        after_epoch = int((value.astimezone(UTC) - timedelta(days=30)).timestamp())
        return _FeedbackCursorState(
            "bootstrap", after_epoch, history_id, None, None, (), 0
        )

    async def _feedback_refs(
        self,
        token: str,
        state: _FeedbackCursorState,
        transport: GmailFeedbackHttpTransport,
    ) -> tuple[tuple[str, ...], _FeedbackCursorState]:
        if state.pending_message_refs:
            return state.pending_message_refs, state
        if state.mode == "bootstrap":
            assert state.after_epoch is not None
            refs, next_page = await transport.list_feedback_messages(
                token=token,
                after_epoch=state.after_epoch,
                page_token=state.page_token,
            )
            if next_page is None:
                continuation = _FeedbackCursorState(
                    "history",
                    None,
                    None,
                    state.boundary_history_id,
                    None,
                    (),
                    0,
                )
            else:
                continuation = _FeedbackCursorState(
                    "bootstrap",
                    state.after_epoch,
                    state.boundary_history_id,
                    None,
                    next_page,
                    (),
                    0,
                )
        else:
            assert state.start_history_id is not None
            refs, next_page, response_history_id = (
                await transport.list_feedback_history(
                    token=token,
                    start_history_id=state.start_history_id,
                    page_token=state.page_token,
                )
            )
            continuation = _FeedbackCursorState(
                "history",
                None,
                None,
                state.start_history_id if next_page is not None else response_history_id,
                next_page,
                (),
                0,
            )
        deduplicated = tuple(dict.fromkeys(refs))
        return deduplicated, continuation

    async def add_label(self, message_ref: str, label: str) -> None:
        raise NotImplementedError


_MAX_HEADER_PREFIX_BYTES = 64 * 1024


def _top_level_report_type(raw_message: bytes) -> str | None:
    """有界前缀（≤64 KiB）解析顶层 Content-Type 的 report-type；绝不扫描正文。

    用 ``BytesHeaderParser`` 正确处理 RFC continuation folding（折行的
    Content-Type/report-type 参数不会漏判）；header/body 分隔不在前缀内
    （超长头或畸形）、无 Content-Type、无 report-type 参数一律返回 None——
    调用方不得据此判定为 complaint。
    """
    prefix = raw_message[:_MAX_HEADER_PREFIX_BYTES]
    separator = len(prefix)
    for marker in (b"\r\n\r\n", b"\n\n"):
        index = prefix.find(marker)
        if index != -1:
            separator = min(separator, index)
    if separator == len(prefix):
        return None
    header_block = prefix[:separator]
    message = BytesHeaderParser(policy=default).parsebytes(header_block)
    if message.get_content_type() != "multipart/report":
        return None
    report_type = message.get_param("report-type")
    if not isinstance(report_type, str):
        return None
    return report_type


def _parse_feedback_message(
    raw_message: bytes, provider_message_ref: str
) -> tuple[EmailFeedbackItem, ...]:
    """按顶层 report-type 分派 DSN/ARF 解析器；普通邮件返回空 tuple。

    必须先于任一解析器的输入校验确定报告类型：oversized structured
    feedback-report 必须交给 ARF 解析器产出 typed MALFORMED（供整页隔离），
    不能先被 DSN 解析器的超大输入校验抛 ValidationError；oversized 普通
    邮件/DSN 也不产生任何事实（不误判 complaint）。report-type 判定只读
    有界前缀（``_top_level_report_type``），对超大 raw 不做整封 MIME 解析。
    """
    if _top_level_report_type(raw_message) == "feedback-report":
        return parse_abuse_report(
            raw_message,
            provider_ref_digest=provider_ref_digest(provider_message_ref),
            occurred_at=message_occurred_at(raw_message),
        )
    if len(raw_message) > MAX_FEEDBACK_MIME_BYTES:
        # 超大非 feedback-report：交给 DSN 解析器只会触发其输入校验
        # ValidationError（无 typed 隔离语义）；这里按普通邮件语义返回
        # 空事实，绝不误判成 complaint。
        return ()
    return parse_delivery_status(raw_message, provider_message_ref)


def _encode_feedback_cursor(state: _FeedbackCursorState) -> str:
    payload = {
        "after_epoch": state.after_epoch,
        "boundary_history_id": state.boundary_history_id,
        "mode": state.mode,
        "page_token": state.page_token,
        "pending_block_offset": state.pending_block_offset,
        "pending_message_refs": list(state.pending_message_refs),
        "start_history_id": state.start_history_id,
        "version": 1,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    cursor = f"gfc1.{encoded}"
    if len(cursor) > 32768:
        raise ValidationError("Gmail feedback cursor 无效")
    return cursor


def _decode_feedback_cursor(cursor: object) -> _FeedbackCursorState:
    if (
        not isinstance(cursor, str)
        or not cursor.startswith("gfc1.")
        or not 6 <= len(cursor) <= 32768
    ):
        raise ValidationError("Gmail feedback cursor 无效")
    try:
        encoded = cursor.removeprefix("gfc1.")
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ValidationError("Gmail feedback cursor 无效") from None
    expected_keys = {
        "after_epoch",
        "boundary_history_id",
        "mode",
        "page_token",
        "pending_block_offset",
        "pending_message_refs",
        "start_history_id",
        "version",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValidationError("Gmail feedback cursor 无效")
    refs = payload["pending_message_refs"]
    strings = (
        payload["boundary_history_id"],
        payload["page_token"],
        payload["start_history_id"],
    )
    if (
        payload["version"] != 1
        or payload["mode"] not in {"bootstrap", "history"}
        or not isinstance(refs, list)
        or len(refs) > 100
        or not all(
            isinstance(value, str)
            and 1 <= len(value) <= 200
            and all(32 < ord(char) < 127 for char in value)
            for value in refs
        )
        or not all(
            value is None
            or (
                isinstance(value, str)
                and 1 <= len(value) <= 200
                and all(32 < ord(char) < 127 for char in value)
            )
            for value in strings
        )
        or not isinstance(payload["pending_block_offset"], int)
        or isinstance(payload["pending_block_offset"], bool)
        or not 0 <= payload["pending_block_offset"] <= 99
        or (
            payload["after_epoch"] is not None
            and (
                not isinstance(payload["after_epoch"], int)
                or isinstance(payload["after_epoch"], bool)
                or payload["after_epoch"] < 0
            )
        )
    ):
        raise ValidationError("Gmail feedback cursor 无效")
    state = _FeedbackCursorState(
        payload["mode"],
        payload["after_epoch"],
        payload["boundary_history_id"],
        payload["start_history_id"],
        payload["page_token"],
        tuple(refs),
        payload["pending_block_offset"],
    )
    bootstrap_valid = (
        state.after_epoch is not None
        and state.boundary_history_id is not None
        and state.start_history_id is None
    )
    history_valid = (
        state.after_epoch is None
        and state.boundary_history_id is None
        and state.start_history_id is not None
    )
    if (state.mode == "bootstrap") != bootstrap_valid or (
        state.mode == "history"
    ) != history_valid:
        raise ValidationError("Gmail feedback cursor 无效")
    return state


def _build_message(request: GmailSendRequest) -> bytes:
    message = EmailMessage(policy=SMTP)
    message["From"] = request.from_address
    message["To"] = request.recipient_address
    message["Subject"] = request.subject
    message["Message-ID"] = f"<{request.deterministic_message_id}>"
    message["X-TradeOS-Idempotency-V1"] = request.idempotency_header
    message["List-Unsubscribe"] = f"<{request.unsubscribe_url}>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.set_content(request.body)
    return message.as_bytes()


def _build_transactional_message(request: GmailTransactionalSendRequest) -> bytes:
    message = EmailMessage(policy=SMTP)
    message["From"] = request.from_address
    message["To"] = request.recipient_address
    message["Subject"] = request.subject
    message["Message-ID"] = f"<{request.deterministic_message_id}>"
    message["X-TradeOS-Idempotency-V1"] = request.idempotency_header
    message.set_content(request.body)
    return message.as_bytes()


def _classify_transport_error(
    error: GmailHttpStatusError | GmailNetworkError,
) -> ToolGatewayError:
    if isinstance(error, GmailNetworkError):
        category = (
            ToolErrorCategory.RECONCILIATION_REQUIRED
            if error.may_have_written
            else ToolErrorCategory.PROVIDER_TRANSIENT
        )
        return ToolGatewayError(category)
    if error.status_code in {401, 403}:
        return ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
    if error.status_code == 429:
        return ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED,
            retry_after_seconds=error.retry_after_seconds,
        )
    retryable_status = error.status_code in {408, 409, 425} or error.status_code >= 500
    if error.may_have_written and retryable_status:
        return ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
    if retryable_status:
        return ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
    return ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)


def _classify_feedback_transport_error(
    error: GmailHttpStatusError | GmailNetworkError,
) -> ToolGatewayError:
    classified = _classify_transport_error(error)
    if classified.category is ToolErrorCategory.RATE_LIMITED:
        retry_after = (
            error.feedback_retry_after_seconds
            if isinstance(error, GmailHttpStatusError)
            else None
        )
        return ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED,
            retry_after_seconds=retry_after,
        )
    return classified


def _classify_transactional_transport_error(
    error: GmailHttpStatusError | GmailNetworkError,
) -> ToolGatewayError:
    classified = _classify_transport_error(error)
    if classified.category is not ToolErrorCategory.RATE_LIMITED:
        return classified
    retry_after = None
    if isinstance(error, GmailHttpStatusError):
        candidate = (
            error.transactional_retry_after_seconds
            if error.transactional_retry_after_seconds is not None
            else error.retry_after_seconds
        )
        if candidate is not None and 1 <= candidate <= 3_600:
            retry_after = candidate
    return ToolGatewayError(
        ToolErrorCategory.RATE_LIMITED,
        retry_after_seconds=retry_after,
    )


def _validate_mailbox(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > 254
        or _MAILBOX_RE.fullmatch(value) is None
    ):
        raise ValidationError("Gmail mailbox 无效")
    local_part, _domain = value.rsplit("@", 1)
    if local_part.startswith(".") or local_part.endswith(".") or ".." in local_part:
        raise ValidationError("Gmail mailbox 无效")


def _validate_text(value: object, *, max_bytes: int, allow_newlines: bool) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > max_bytes:
        raise ValidationError("Gmail 文本无效")
    for char in value:
        codepoint = ord(char)
        if codepoint == 0 or codepoint == 127 or (codepoint < 32 and char not in "\n\t"):
            raise ValidationError("Gmail 文本无效")
        if not allow_newlines and char in "\r\n":
            raise ValidationError("Gmail 文本无效")
    if "\r" in value:
        raise ValidationError("Gmail 文本无效")


def _validate_https_url(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or len(value.encode("utf-8")) > 2_048
    ):
        raise ValidationError("Gmail 退订链接无效")
    try:
        parsed = urlparse(value)
        is_loopback_http = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        )
    except ValueError:
        raise ValidationError("Gmail 退订链接无效") from None
    if (
        (parsed.scheme != "https" and not is_loopback_http)
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.netloc != parsed.netloc.casefold()
        or any(char.isspace() for char in value)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValidationError("Gmail 退订链接无效")


def _validate_safe_id(value: object, message: str) -> None:
    if (
        not isinstance(value, str)
        or _SAFE_ID_RE.fullmatch(value) is None
        or any(marker in value.casefold() for marker in _SECRET_MARKERS)
    ):
        raise ValidationError(message)


def _credential_shaped(value: object) -> bool:
    if not isinstance(value, str):
        return True
    lowered = value.casefold()
    return any(marker in lowered for marker in _SECRET_MARKERS) or bool(
        re.search(
            r"(?:password|token|secret|authorization|bearer)\s*[:=]",
            lowered,
        )
    )
