"""阿里企业邮箱连接器：只替换 SMTP/IMAP 外部边界。"""

from __future__ import annotations

import imaplib
import smtplib
import ssl
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default
from typing import Any

import pytest
from pydantic import SecretStr

from connectors.gmail.client import GmailSendRequest
from shared.errors import ValidationError
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError

ADDRESS = "sales@example.com"
PASSWORD = SecretStr("fixture-client-password")


def request() -> GmailSendRequest:
    return GmailSendRequest(
        from_address=ADDRESS,
        recipient_address="buyer@example.org",
        subject="Product enquiry",
        body="Hello,\nWould these parts fit your application?",
        unsubscribe_url="https://example.com/unsubscribe/fixture",
        deterministic_message_id="tradeos-fixture@example.com",
        idempotency_header="fixture-idempotency-1",
    )


class MailServer:
    """外部协议替身保留真实线上的状态与返回形状。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.saved: list[bytes] = []
        self.sent: list[bytes] = []
        self.search_ids = b""
        self.headers: bytes | None = None
        self.smtp_error: Exception | None = None
        self.imap_auth_error = False
        self.append_error = False
        self.select_status = "OK"
        self.listing = [b'(\\HasNoChildren \\Sent) "/" "Sent"']
        self.data_code = 250
        self.mail_code = 250

    def open_smtp(self) -> MailServer:
        self.calls.append(("smtp",))
        return self

    def open_imap(self) -> MailServer:
        self.calls.append(("imap",))
        return self

    def login(self, address: str, password: str) -> tuple[Any, Any]:
        self.calls.append(("login", address))
        assert password == PASSWORD.get_secret_value()
        if self.imap_auth_error:
            raise imaplib.IMAP4.error("sensitive provider text")
        return "OK", [b"authenticated"]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", self.listing

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        return self.select_status, [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [self.search_ids]
        assert command == "FETCH"
        return "OK", [(b"1 (UID 91 BODY[HEADER.FIELDS] {100}", self.headers), b")"]

    def mail(self, address: str) -> tuple[int, bytes]:
        self.calls.append(("mail", address))
        return self.mail_code, b"provider text"

    def rcpt(self, address: str) -> tuple[int, bytes]:
        self.calls.append(("rcpt", address))
        return 250, b"provider text"

    def data(self, raw: bytes) -> tuple[int, bytes]:
        self.sent.append(raw)
        if self.smtp_error is not None:
            raise self.smtp_error
        return self.data_code, b"provider-secret-response"

    def append(
        self, mailbox: str, flags: str, date_time: Any, message: bytes
    ) -> tuple[str, list[bytes]]:
        assert self.sent, "不能在 SMTP 接受前制造 Sent 记录"
        if self.append_error:
            raise OSError("sensitive provider text")
        self.saved.append(message)
        return "OK", [b"APPENDUID 123 91"]

    def close(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


def connector(server: MailServer | None = None, *, address: str = ADDRESS) -> Any:
    from connectors.aliyun_mail.client import AliyunMailConnector

    return AliyunMailConnector(address, PASSWORD, transport=server or MailServer())


def matching_headers(**overrides: str) -> bytes:
    headers = {
        "From": ADDRESS,
        "To": "buyer@example.org",
        "Subject": "Product enquiry",
        "Message-ID": "<tradeos-fixture@example.com>",
        "X-TradeOS-Idempotency-V1": "fixture-idempotency-1",
    }
    headers.update(overrides)
    return (
        "\r\n".join(f"{key}: {value}" for key, value in headers.items()) + "\r\n\r\n"
    ).encode()


@pytest.mark.asyncio
async def test_send_builds_mime_and_appends_only_after_smtp_acceptance() -> None:
    server = MailServer()
    result = await connector(server).send_once(request())
    assert result.certainty is DeliveryCertainty.SENT
    assert result.provider_ref.startswith("aliyun_")
    assert result.already_existed is False
    assert len(server.sent) == 1
    message = BytesParser(policy=default).parsebytes(server.sent[0])
    assert str(message["From"]) == ADDRESS
    assert str(message["To"]) == "buyer@example.org"
    assert str(message["Message-ID"]) == "<tradeos-fixture@example.com>"
    assert str(message["X-TradeOS-Idempotency-V1"]) == "fixture-idempotency-1"
    assert (
        str(message["List-Unsubscribe"]) == "<https://example.com/unsubscribe/fixture>"
    )
    assert str(message["List-Unsubscribe-Post"]) == "List-Unsubscribe=One-Click"
    assert message.get_content().replace("\r\n", "\n").rstrip() == request().body
    assert server.saved == server.sent
    assert ("mail", ADDRESS) in server.calls
    assert ("rcpt", "buyer@example.org") in server.calls
    assert ("select", '"Sent"', True) in server.calls


@pytest.mark.asyncio
async def test_authentication_checks_both_protocols_without_sending() -> None:
    server = MailServer()
    assert await connector(server).authenticate() is None
    assert ("imap",) in server.calls and ("smtp",) in server.calls
    assert server.sent == server.saved == []


@pytest.mark.asyncio
async def test_rejects_different_sender_before_any_io() -> None:
    server = MailServer()
    with pytest.raises(ValidationError):
        await connector(server).send_once(
            replace(request(), from_address="other@example.com")
        )
    assert server.calls == []


@pytest.mark.parametrize(
    "address",
    [
        "a@example.com\r\nBcc: victim@example.org",
        "User <a@example.com>",
        "a@example.com,b@example.com",
        "",
        "a@localhost",
    ],
)
def test_rejects_invalid_bound_mailbox(address: str) -> None:
    with pytest.raises(ValidationError):
        connector(address=address)


@pytest.mark.asyncio
async def test_existing_exact_sent_headers_skip_smtp_and_keep_stable_ref() -> None:
    server = MailServer()
    first = await connector(server).send_once(request())
    server.search_ids = b"91"
    server.headers = matching_headers()
    result = await connector(server).send_once(request())
    assert result.already_existed is True
    assert result.provider_ref == first.provider_ref
    assert len(server.sent) == 1
    searches = [item for item in server.calls if item[0] == "SEARCH"]
    assert all(
        "Message-ID" in item and "X-TradeOS-Idempotency-V1" in item for item in searches
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"From": "attacker@example.org"},
        {"To": "other@example.org"},
        {"Message-ID": "<different@example.com>"},
        {"X-TradeOS-Idempotency-V1": "wrong-key"},
    ],
)
@pytest.mark.asyncio
async def test_search_substring_or_wrong_envelope_is_not_delivery_proof(
    changed: dict[str, str],
) -> None:
    server = MailServer()
    server.search_ids = b"91"
    server.headers = matching_headers(**changed)
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).reconcile_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert server.sent == []


@pytest.mark.asyncio
async def test_missing_reconciliation_stays_unknown_without_smtp() -> None:
    server = MailServer()
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).reconcile_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert ("smtp",) not in server.calls


@pytest.mark.asyncio
async def test_timeout_after_data_is_unknown_and_never_appended_or_retried() -> None:
    server = MailServer()
    server.smtp_error = TimeoutError("provider-secret-response")
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert "provider-secret-response" not in str(caught.value)
    assert len(server.sent) == 1 and server.saved == []


@pytest.mark.asyncio
async def test_append_failure_does_not_erase_confirmed_smtp_acceptance() -> None:
    server = MailServer()
    server.append_error = True
    result = await connector(server).send_once(request())
    assert (
        result.certainty is DeliveryCertainty.SENT and result.provider_ref is not None
    )
    assert len(server.sent) == 1 and server.saved == []


@pytest.mark.asyncio
async def test_unavailable_sent_mailbox_blocks_before_smtp() -> None:
    server = MailServer()
    server.select_status = "NO"
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.PROVIDER_PERMANENT
    assert server.sent == [] and ("smtp",) not in server.calls


@pytest.mark.parametrize(
    "code, category",
    [
        (450, ToolErrorCategory.PROVIDER_TRANSIENT),
        (550, ToolErrorCategory.PROVIDER_PERMANENT),
    ],
)
@pytest.mark.asyncio
async def test_explicit_smtp_rejection_is_definitely_failed(
    code: int, category: ToolErrorCategory
) -> None:
    server = MailServer()
    server.data_code = code
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is category
    assert server.saved == []


@pytest.mark.asyncio
async def test_auth_error_is_fixed_and_does_not_leak_provider_text() -> None:
    server = MailServer()
    server.imap_auth_error = True
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).authenticate()
    assert caught.value.category is ToolErrorCategory.PROVIDER_AUTH_REQUIRED
    assert "sensitive" not in str(caught.value)
    assert PASSWORD.get_secret_value() not in repr(connector(server))


def test_production_transport_uses_fixed_tls_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from connectors.aliyun_mail.client import DefaultAliyunMailTransport

    calls: list[tuple[Any, ...]] = []

    def smtp(*args: Any, **kwargs: Any) -> Any:
        calls.append((*args, kwargs))
        return object()

    def imap(*args: Any, **kwargs: Any) -> Any:
        calls.append((*args, kwargs))
        return object()

    monkeypatch.setattr("connectors.aliyun_mail.client.smtplib.SMTP_SSL", smtp)
    monkeypatch.setattr("connectors.aliyun_mail.client.BoundedIMAP4SSL", imap)
    transport = DefaultAliyunMailTransport()
    transport.open_smtp()
    transport.open_imap()
    assert calls[0][:2] == ("smtp.qiye.aliyun.com", 465)
    assert calls[1][:2] == ("imap.qiye.aliyun.com", 993)
    for entry in calls:
        options = entry[-1]
        context = options.get("context", options.get("ssl_context"))
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert 10 <= options["timeout"] <= 20


@pytest.mark.parametrize(
    "address", [".sales@example.com", "sales.@example.com", "a..b@example.com"]
)
def test_bound_local_part_rejects_invalid_dots(address: str) -> None:
    with pytest.raises(ValidationError):
        connector(address=address)


@pytest.mark.asyncio
async def test_known_chinese_sent_name_works_without_special_use_flag() -> None:
    server = MailServer()
    server.listing = [b'(\\HasNoChildren) "/" "&XfJT0ZAB-"']
    result = await connector(server).send_once(request())
    assert result.certainty is DeliveryCertainty.SENT
    assert ("select", '"&XfJT0ZAB-"', True) in server.calls


@pytest.mark.parametrize("code", [354, 999])
@pytest.mark.asyncio
async def test_unexpected_data_reply_cannot_prove_non_delivery(code: int) -> None:
    server = MailServer()
    server.data_code = code
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert server.saved == []


@pytest.mark.asyncio
async def test_explicit_data_exception_has_safe_permanent_classification() -> None:
    server = MailServer()
    server.smtp_error = smtplib.SMTPDataError(550, b"sensitive provider error")
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.PROVIDER_PERMANENT
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_rejected_sender_never_submits_data() -> None:
    server = MailServer()
    server.mail_code = 550
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.PROVIDER_PERMANENT
    assert server.sent == server.saved == []


@pytest.mark.asyncio
async def test_duplicate_identity_headers_do_not_prove_sent() -> None:
    server = MailServer()
    server.search_ids = b"91"
    server.headers = b"From: attacker@example.org\r\n" + matching_headers()
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert server.sent == []


@pytest.mark.parametrize("kind", ["matches", "headers", "folders"])
@pytest.mark.asyncio
async def test_response_limits_fail_closed_before_smtp(kind: str) -> None:
    server = MailServer()
    if kind == "matches":
        server.search_ids = b" ".join(str(i).encode() for i in range(1, 22))
    elif kind == "headers":
        server.search_ids = b"91"
        server.headers = b"a" * 32769
    else:
        server.listing *= 65
    with pytest.raises(ToolGatewayError):
        await connector(server).send_once(request())
    assert server.sent == []
