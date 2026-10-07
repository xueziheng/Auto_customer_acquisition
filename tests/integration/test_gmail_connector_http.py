"""Gmail HTTP transport 的受控本地网络合同。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import pytest

import connectors.gmail.client as gmail_client
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError


class _State:
    existing: str | None = None
    status = 200
    retry_after: str | None = None
    searches: ClassVar[list[dict[str, list[str]]]] = []
    sent: ClassVar[list[dict[str, str]]] = []
    metadata_headers: ClassVar[dict[str, str]] = {}
    redirect_location: str | None = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _error(self) -> bool:
        if _State.status == 200:
            return False
        self.send_response(_State.status)
        if _State.redirect_location is not None:
            self.send_header("Location", _State.redirect_location)
        if _State.retry_after is not None:
            self.send_header("Retry-After", _State.retry_after)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"provider-body-marker"}')
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        _State.searches.append(query)
        if self._error():
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if "/messages/" in parsed.path:
            payload = {
                "id": _State.existing,
                "payload": {
                    "headers": [
                        {"name": name, "value": value}
                        for name, value in _State.metadata_headers.items()
                    ]
                },
            }
        else:
            payload = {
                "messages": []
                if _State.existing is None
                else [{"id": _State.existing}]
            }
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        _State.sent.append(json.loads(self.rfile.read(size)))
        if self._error():
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"gmail_http_ref_01"}')


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "GMAIL_OAUTH_TOKEN_REF"
        return "http-oauth-marker"


@pytest.fixture
def gmail_server():
    _State.existing = None
    _State.status = 200
    _State.retry_after = None
    _State.searches = []
    _State.sent = []
    _State.metadata_headers = {}
    _State.redirect_location = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request():
    return gmail_client.GmailSendRequest(
        from_address="sender@example.com",
        recipient_address="buyer@example.net",
        subject="Subject marker",
        body="Body marker",
        unsubscribe_url="https://example.com/unsubscribe/ref_01",
        deterministic_message_id="mat_01@messages.tradeos.invalid",
        idempotency_header="idem_01",
    )


@pytest.mark.asyncio
async def test_local_http_search_then_send_and_existing_short_circuit(gmail_server: str) -> None:
    transport_cls = __import__("connectors.gmail.transport", fromlist=["GmailApiHttpTransport"]).GmailApiHttpTransport
    connector = gmail_client.GmailConnector(transport_cls(gmail_server))
    await connector.configure(_Secrets())
    first = await connector.send_once(_request())
    assert first.certainty is DeliveryCertainty.SENT
    assert len(_State.sent) == 1
    assert "raw" in _State.sent[0]

    _State.existing = "gmail_existing_http_01"
    _State.metadata_headers = {
        "Message-ID": "<mat_01@messages.tradeos.invalid>",
        "X-TradeOS-Idempotency-V1": "idem_01",
    }
    second = await connector.send_once(_request())
    assert second.already_existed is True
    assert second.provider_ref == "gmail_existing_http_01"
    assert len(_State.sent) == 1
    assert _State.searches[0]["q"] == [
        "rfc822msgid:mat_01@messages.tradeos.invalid"
    ]
    assert _State.searches[-1]["metadataHeaders"] == [
        "Message-ID",
        "X-TradeOS-Idempotency-V1",
    ]


@pytest.mark.asyncio
async def test_list_candidate_with_mismatched_custom_header_does_not_short_circuit(
    gmail_server: str,
) -> None:
    _State.existing = "gmail_candidate_01"
    _State.metadata_headers = {
        "Message-ID": "<mat_01@messages.tradeos.invalid>",
        "X-TradeOS-Idempotency-V1": "different",
    }
    transport_cls = __import__(
        "connectors.gmail.transport", fromlist=["GmailApiHttpTransport"]
    ).GmailApiHttpTransport
    connector = gmail_client.GmailConnector(transport_cls(gmail_server))
    await connector.configure(_Secrets())
    result = await connector.send_once(_request())
    assert result.already_existed is False
    assert result.provider_ref == "gmail_http_ref_01"
    assert len(_State.sent) == 1


@pytest.mark.parametrize(
    ("status", "retry_after", "category", "bounded"),
    [
        (401, None, ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
        (403, None, ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
        (429, "31", ToolErrorCategory.RATE_LIMITED, 31),
        (429, "999999", ToolErrorCategory.RATE_LIMITED, 86_400),
        (400, None, ToolErrorCategory.PROVIDER_PERMANENT, None),
        (500, None, ToolErrorCategory.PROVIDER_TRANSIENT, None),
    ],
)
@pytest.mark.asyncio
async def test_local_http_search_errors_are_typed_and_provider_body_is_hidden(
    gmail_server: str, status: int, retry_after: str | None, category, bounded
) -> None:
    _State.status = status
    _State.retry_after = retry_after
    transport_cls = __import__("connectors.gmail.transport", fromlist=["GmailApiHttpTransport"]).GmailApiHttpTransport
    connector = gmail_client.GmailConnector(transport_cls(gmail_server))
    await connector.configure(_Secrets())
    with pytest.raises(ToolGatewayError) as caught:
        await connector.send_once(_request())
    assert caught.value.category is category
    assert caught.value.retry_after_seconds == bounded
    assert "provider-body-marker" not in str(caught.value)
    assert "http-oauth-marker" not in str(caught.value)


@pytest.mark.asyncio
async def test_send_5xx_after_body_requires_reconciliation(gmail_server: str) -> None:
    transport_cls = __import__("connectors.gmail.transport", fromlist=["GmailApiHttpTransport"]).GmailApiHttpTransport
    connector = gmail_client.GmailConnector(transport_cls(gmail_server))
    await connector.configure(_Secrets())
    original_search = connector._transport.search

    async def search_then_fail(**kwargs):
        result = await original_search(**kwargs)
        _State.status = 500
        return result

    connector._transport.search = search_then_fail  # type: ignore[method-assign]
    with pytest.raises(ToolGatewayError) as caught:
        await connector.send_once(_request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert len(_State.sent) == 1


@pytest.mark.asyncio
async def test_http_redirect_is_not_followed_with_oauth_header(gmail_server: str) -> None:
    _State.status = 302
    _State.redirect_location = f"{gmail_server}/credential-sink"
    transport_cls = __import__(
        "connectors.gmail.transport", fromlist=["GmailApiHttpTransport"]
    ).GmailApiHttpTransport
    connector = gmail_client.GmailConnector(transport_cls(gmail_server))
    await connector.configure(_Secrets())
    with pytest.raises(ToolGatewayError) as caught:
        await connector.send_once(_request())
    assert caught.value.category is ToolErrorCategory.PROVIDER_TRANSIENT
    assert len(_State.searches) == 1
