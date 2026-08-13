"""Gmail 单封发送连接器。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from connectors.base import ConnectorManifest
from shared.errors import ValidationError
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError

from .transport import GmailHttpStatusError, GmailHttpTransport, GmailNetworkError

MANIFEST = ConnectorManifest(
    connector_id="gmail",
    capabilities=(
        "email.send",
        "email.fetch_replies",
        "email.parse_bounce",
        "email.add_label",
        "dns.check_auth",
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


@dataclass(frozen=True)
class BounceEvent:
    """解析出的退信事件。"""

    original_message_ref: str
    is_hard: bool
    needs_review: bool
    raw_artifact_ref: str
    occurred_at: datetime
    dedup_key: str


class GmailConnector:
    manifest = MANIFEST

    def __init__(self, transport: GmailHttpTransport) -> None:
        if not isinstance(transport, GmailHttpTransport):
            raise ValidationError("Gmail transport 无效")
        self._transport = transport
        self._token: _SecretValue | None = None

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

    async def fetch_new_messages(
        self, since_cursor: str | None
    ) -> tuple[list[dict], str]:
        raise NotImplementedError

    async def parse_bounce(self, raw_message: dict) -> BounceEvent | None:
        raise NotImplementedError

    async def add_label(self, message_ref: str, label: str) -> None:
        raise NotImplementedError

    async def check_dns_auth(self, domain: str) -> dict[str, bool]:
        raise NotImplementedError


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
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
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
