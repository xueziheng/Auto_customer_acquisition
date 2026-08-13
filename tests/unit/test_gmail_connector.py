"""Gmail 单封发送连接器的安全合同。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

import connectors.gmail.client as gmail_client
from shared.errors import ValidationError
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError


class _Secrets:
    def __init__(self, value: str = "oauth-marker") -> None:
        self.value = value
        self.refs: list[str] = []

    def resolve(self, secret_ref: str) -> str:
        self.refs.append(secret_ref)
        return self.value


class _Transport:
    def __init__(self) -> None:
        self.existing: str | None = None
        self.sent_ref = "gmail_ref_01"
        self.search_error: BaseException | None = None
        self.send_error: BaseException | None = None
        self.searches: list[tuple[str, str, str]] = []
        self.sends: list[tuple[str, bytes]] = []

    async def search(self, *, token: str, message_id: str, header: str) -> str | None:
        self.searches.append((token, message_id, header))
        if self.search_error is not None:
            raise self.search_error
        return self.existing

    async def send(self, *, token: str, raw_message: bytes) -> str:
        self.sends.append((token, raw_message))
        if self.send_error is not None:
            raise self.send_error
        return self.sent_ref


def _request(**changes: object):
    cls = gmail_client.GmailSendRequest
    values: dict[str, object] = {
        "from_address": "sender@example.com",
        "recipient_address": "buyer@example.net",
        "subject": "TradeOS follow-up",
        "body": "Hello buyer.\n",
        "unsubscribe_url": "https://example.com/unsubscribe/ref_01",
        "deterministic_message_id": "mat_01@messages.tradeos.invalid",
        "idempotency_header": "idem_01",
    }
    values.update(changes)
    return cls(**values)


def _transport_symbol(name: str):
    module = __import__("connectors.gmail.transport", fromlist=[name])
    return getattr(module, name)


def test_request_and_result_hide_raw_material_and_are_frozen() -> None:
    request = _request()
    rendered = repr(request)
    for marker in ("sender@example.com", "buyer@example.net", "TradeOS", "Hello", "unsubscribe"):
        assert marker not in rendered
    with pytest.raises(FrozenInstanceError):
        request.body = "changed"  # type: ignore[misc]

    result_cls = gmail_client.GmailSendResult
    result = result_cls("gmail_ref_01", DeliveryCertainty.SENT, False)
    assert result.provider_ref == "gmail_ref_01"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_address", "Display <sender@example.com>"),
        ("from_address", "sender@Example.com"),
        ("from_address", ".sender@example.com"),
        ("from_address", "sender.@example.com"),
        ("from_address", "send..er@example.com"),
        ("recipient_address", "buyer@example.com\r\nBcc: leak@example.com"),
        ("recipient_address", "buyer @example.com"),
        ("subject", "hello\rworld"),
        ("subject", "x" * 999),
        ("body", "body\x00marker"),
        ("body", "x" * 1_048_577),
        ("unsubscribe_url", "http://example.com/unsubscribe"),
        ("unsubscribe_url", "https://user:pass@example.com/u"),
        ("unsubscribe_url", "https://EXAMPLE.com/unsubscribe"),
        ("unsubscribe_url", "https://example.com/has space"),
        ("unsubscribe_url", "https://example.com/退订"),
        ("deterministic_message_id", "<unsafe@example.com>"),
        ("idempotency_header", "token-secret"),
    ],
)
def test_request_rejects_noncanonical_or_unsafe_wire_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _request(**{field: value})
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    ("http://127.0.0.1:8000/unsubscribe/ref", "http://localhost:8000/u/ref"),
)
def test_request_accepts_dev_loopback_unsubscribe_url(value: str) -> None:
    assert _request(unsubscribe_url=value).unsubscribe_url == value


@pytest.mark.parametrize("provider_ref", ["", "has space", "Bearer_abc", "x" * 201])
def test_result_rejects_unsafe_provider_reference(provider_ref: str) -> None:
    result_cls = gmail_client.GmailSendResult
    with pytest.raises(ValidationError):
        result_cls(provider_ref, DeliveryCertainty.SENT, False)


@pytest.mark.asyncio
async def test_configure_resolves_only_declared_ref_and_search_hit_skips_send() -> None:
    transport = _Transport()
    transport.existing = "gmail_existing_01"
    secrets = _Secrets()
    connector = gmail_client.GmailConnector(transport)
    await connector.configure(secrets)
    result = await connector.send_once(_request())

    assert secrets.refs == ["GMAIL_OAUTH_TOKEN_REF"]
    assert result == gmail_client.GmailSendResult(
        "gmail_existing_01", DeliveryCertainty.SENT, True
    )
    assert len(transport.searches) == 1
    assert transport.sends == []
    assert "oauth-marker" not in repr(connector)


@pytest.mark.asyncio
async def test_search_miss_sends_exact_rfc5322_headers_once() -> None:
    transport = _Transport()
    connector = gmail_client.GmailConnector(transport)
    await connector.configure(_Secrets())
    result = await connector.send_once(_request())

    assert result.certainty is DeliveryCertainty.SENT
    assert result.already_existed is False
    assert len(transport.sends) == 1
    raw = transport.sends[0][1]
    assert b"Message-ID: <mat_01@messages.tradeos.invalid>" in raw
    assert b"X-TradeOS-Idempotency-V1: idem_01" in raw
    assert b"List-Unsubscribe: <https://example.com/unsubscribe/ref_01>" in raw
    assert b"List-Unsubscribe-Post: List-Unsubscribe=One-Click" in raw


@pytest.mark.parametrize(
    ("error", "category", "retry_after"),
    [
        (lambda: _transport_symbol("GmailHttpStatusError")(401, may_have_written=False), ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
        (lambda: _transport_symbol("GmailHttpStatusError")(403, may_have_written=False), ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
        (lambda: _transport_symbol("GmailHttpStatusError")(429, retry_after_seconds=17, may_have_written=False), ToolErrorCategory.RATE_LIMITED, 17),
        (lambda: _transport_symbol("GmailHttpStatusError")(400, may_have_written=False), ToolErrorCategory.PROVIDER_PERMANENT, None),
        (lambda: _transport_symbol("GmailNetworkError")(may_have_written=False), ToolErrorCategory.PROVIDER_TRANSIENT, None),
    ],
)
@pytest.mark.asyncio
async def test_search_error_classification_is_typed_and_safe(error, category, retry_after) -> None:
    transport = _Transport()
    transport.search_error = error()
    connector = gmail_client.GmailConnector(transport)
    await connector.configure(_Secrets())
    with pytest.raises(ToolGatewayError) as caught:
        await connector.send_once(_request())
    assert caught.value.category is category
    assert caught.value.retry_after_seconds == retry_after
    for marker in ("oauth-marker", "buyer@example.net", "Hello buyer"):
        assert marker not in str(caught.value)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (lambda: _transport_symbol("GmailNetworkError")(may_have_written=True), ToolErrorCategory.RECONCILIATION_REQUIRED),
        (lambda: _transport_symbol("GmailHttpStatusError")(500, may_have_written=True), ToolErrorCategory.RECONCILIATION_REQUIRED),
        (lambda: _transport_symbol("GmailHttpStatusError")(408, may_have_written=True), ToolErrorCategory.RECONCILIATION_REQUIRED),
        (lambda: _transport_symbol("GmailHttpStatusError")(409, may_have_written=True), ToolErrorCategory.RECONCILIATION_REQUIRED),
        (lambda: _transport_symbol("GmailHttpStatusError")(425, may_have_written=True), ToolErrorCategory.RECONCILIATION_REQUIRED),
    ],
)
@pytest.mark.asyncio
async def test_send_maybe_written_failures_require_reconciliation(error, category) -> None:
    transport = _Transport()
    transport.send_error = error()
    connector = gmail_client.GmailConnector(transport)
    await connector.configure(_Secrets())
    with pytest.raises(ToolGatewayError) as caught:
        await connector.send_once(_request())
    assert caught.value.category is category


@pytest.mark.asyncio
async def test_send_connection_failure_before_bytes_is_transient_not_reconciliation() -> None:
    transport = _Transport()
    transport.send_error = _transport_symbol("GmailNetworkError")(
        may_have_written=False
    )
    connector = gmail_client.GmailConnector(transport)
    await connector.configure(_Secrets())
    with pytest.raises(ToolGatewayError) as caught:
        await connector.send_once(_request())
    assert caught.value.category is ToolErrorCategory.PROVIDER_TRANSIENT


def test_request_raw_whitespace_is_preserved_not_normalized() -> None:
    request = _request(body=" line one \nline two  \n")
    assert request != replace(request, body="line one\nline two\n")
