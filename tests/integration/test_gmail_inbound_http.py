"""只受控Provider响应的真实HTTP读取预算及错误分类。"""

import base64
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from connectors.gmail.inbound_transport import GmailInboundApiTransport
from connectors.gmail.transport import GmailHttpStatusError, GmailNetworkError
from shared.schemas.email_inbound import MIME_BYTES
from tests.unit.test_email_inbound import mime


@contextmanager
def server(body, *, status=200, length=True, retry=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/must-not-follow")
            if retry:
                self.send_header("Retry-After", retry)
            if length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for offset in range(0, len(body), 4096):
                    self.wfile.write(body[offset : offset + 4096])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            GmailInboundApiTransport(
                f"http://127.0.0.1:{http.server_port}", controlled_loopback=True
            ),
            requests,
        )
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)
        assert not thread.is_alive()


def envelope(content, **changes):
    return json.dumps(
        {
            "raw": base64.urlsafe_b64encode(content).decode(),
            "labelIds": ["INBOX"],
            "internalDate": "1788598800000",
            **changes,
        }
    ).encode()


@pytest.mark.asyncio
async def test_raw_metadata_and_encoded_message_ref():
    raw = mime()
    with server(envelope(raw)) as (transport, requests):
        result = await transport.get_inbound_message(
            token="controlled", message_ref="id/?&", maximum_bytes=MIME_BYTES
        )
    assert (
        result.status == "raw"
        and result.raw_mime == raw
        and result.labels == ("INBOX",)
    )
    assert result.internal_date is not None
    assert requests == ["/gmail/v1/users/me/messages/id%2F%3F%26?format=raw"]


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [True, False])
async def test_http_overflow_is_permanent_bounded_result(length):
    with server(b"x" * (65536 + 4096), length=length) as (transport, _):
        result = await transport.get_inbound_message(
            token="controlled", message_ref="a", maximum_bytes=100
        )
    assert result.status == "too_large" and result.raw_mime is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"raw": "a!"}, "malformed"),
        ({"labelIds": None}, "malformed"),
        ({"internalDate": "not-a-date"}, "malformed"),
        ({"raw": "a" * 2048}, "too_large"),
    ],
)
async def test_invalid_envelope_has_fixed_disposition(changes, expected):
    with server(envelope(b"x", **changes)) as (transport, _):
        result = await transport.get_inbound_message(
            token="controlled", message_ref="a", maximum_bytes=100
        )
    assert result.status == expected


@pytest.mark.asyncio
async def test_message_404_gone_history_404_not_skipped():
    with server(b"", status=404) as (transport, _):
        assert (
            await transport.get_inbound_message(
                token="controlled", message_ref="a", maximum_bytes=100
            )
        ).status == "message_gone"
        with pytest.raises(GmailHttpStatusError) as error:
            await transport.list_feedback_history(
                token="controlled", start_history_id="10", page_token=None
            )
        assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_status_safe_and_retry_bounded(status):
    with server(b"private-response", status=status, retry="3600") as (transport, _):
        with pytest.raises(GmailHttpStatusError) as error:
            await transport.get_inbound_message(
                token="controlled", message_ref="a", maximum_bytes=100
            )
        assert error.value.status_code == status
        assert error.value.feedback_retry_after_seconds == 3600
        assert "private-response" not in str(error.value) + repr(error.value)


@pytest.mark.asyncio
async def test_redirect_never_followed():
    with server(b"", status=302) as (transport, requests):
        with pytest.raises(GmailNetworkError):
            await transport.get_inbound_message(
                token="controlled", message_ref="a", maximum_bytes=100
            )
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_malformed_json_is_permanent_message_disposition():
    with server(b"not-json") as (transport, _):
        result = await transport.get_inbound_message(
            token="controlled", message_ref="a", maximum_bytes=100
        )
    assert result.status == "malformed"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not-json", b'{"historyId": ""}', b"x" * 70000])
async def test_profile_malformed_or_over_budget_is_permanent(payload):
    from connectors.gmail.transport import GmailMalformedResponse, GmailResponseTooLarge

    with (
        server(payload) as (transport, _),
        pytest.raises((GmailMalformedResponse, GmailResponseTooLarge)),
    ):
        await transport.get_profile_history_id(token="controlled")


@pytest.mark.asyncio
async def test_invalid_provider_ref_is_permanent_not_network_retry():
    from connectors.gmail.transport import GmailMalformedResponse

    with (
        server(b'{"messages":[{"id":""}]}') as (transport, _),
        pytest.raises(GmailMalformedResponse),
    ):
        await transport.list_feedback_messages(
            token="controlled", after_epoch=1, page_token=None
        )


@pytest.mark.asyncio
async def test_connection_failure_retains_transient_network_type():
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    transport = GmailInboundApiTransport(
        f"http://127.0.0.1:{port}", controlled_loopback=True
    )
    with pytest.raises(GmailNetworkError):
        await transport.get_profile_history_id(token="controlled")
