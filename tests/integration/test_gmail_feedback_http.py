"""Gmail feedback 的真实本地 HTTP、cursor 续页和错误分类。"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from connectors.gmail.client import GmailConnector
from connectors.gmail.transport import GmailApiHttpTransport, GmailNetworkError
from shared.errors import ValidationError
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "GMAIL_OAUTH_TOKEN_REF"
        return "oauth-private-marker"


def _dsn(statuses: tuple[str, ...]) -> bytes:
    boundary = "feedback-http-boundary"
    lines = [
        "Date: Thu, 13 Aug 2026 10:00:00 +0000",
        f'Content-Type: multipart/report; report-type="delivery-status"; boundary="{boundary}"',
        "MIME-Version: 1.0",
        "",
        f"--{boundary}",
        "Content-Type: text/plain",
        "",
        "ignored body",
        f"--{boundary}",
        "Content-Type: message/delivery-status",
        "",
        "Reporting-MTA: dns; mx.example.invalid",
        "",
    ]
    for index, status in enumerate(statuses):
        lines.extend(
            [
                f"Final-Recipient: rfc822; private-{index}@example.com",
                f"Status: {status}",
                "",
            ]
        )
    lines.extend(
        [
            f"--{boundary}",
            "Content-Type: message/rfc822",
            "",
            "Message-ID: <" + "a" * 64 + "@messages.tradeos.invalid>",
            "X-TradeOS-Idempotency-V1: " + "b" * 64,
            "",
            f"--{boundary}--",
            "",
        ]
    )
    return "\r\n".join(lines).encode("ascii")


def _raw_payload(raw: bytes) -> bytes:
    return json.dumps(
        {"raw": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")},
        separators=(",", ":"),
    ).encode("ascii")


def _arf_report(
    *,
    date_header: str | None = "Date: Thu, 13 Aug 2026 10:00:00 +0000",
    report_pad: int = 0,
    folded_content_type: bool = False,
) -> bytes:
    boundary = "feedback-http-arf"
    lines = []
    if date_header is not None:
        lines.append(date_header)
    if folded_content_type:
        lines.append('Content-Type: multipart/report;')
        lines.append(' report-type="feedback-report";')
        lines.append(f' boundary="{boundary}"')
    else:
        lines.append(
            'Content-Type: multipart/report; report-type="feedback-report"; '
            f'boundary="{boundary}"'
        )
    lines.extend(
        [
            "MIME-Version: 1.0",
            "",
            f"--{boundary}",
            "Content-Type: text/plain",
            "",
            "complaint prose",
            f"--{boundary}",
            "Content-Type: message/feedback-report",
            "",
            "Feedback-Type: abuse",
            "User-Agent: provider-test/1.0",
            "Version: 1",
            "",
        ]
    )
    if report_pad:
        lines.append("x" * report_pad)
        lines.append("")
    lines.extend(
        [
            f"--{boundary}",
            "Content-Type: message/rfc822",
            "",
            "Message-ID: <route-v1." + "a" * 64 + "@messages.tradeos.invalid>",
            "X-TradeOS-Idempotency-V1: route-v1." + "b" * 64,
            "",
            f"--{boundary}--",
            "",
        ]
    )
    return "\r\n".join(lines).encode("ascii")


class _Scenario:
    def __init__(self, responses: list[tuple[int, dict[str, str], bytes]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, list[str]], str | None]] = []


@contextmanager
def _server(
    responses: list[tuple[int, dict[str, str], bytes]],
) -> Iterator[tuple[str, _Scenario]]:
    scenario = _Scenario(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            scenario.requests.append(
                (parsed.path, parse_qs(parsed.query), self.headers.get("Authorization"))
            )
            status, headers, body = scenario.responses.pop(0)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", scenario
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_arf_complaint_fetches_typed_complaint_item() -> None:
    responses = [
        (200, {}, b'{"historyId":"100"}'),
        (200, {}, b'{"messages":[{"id":"m1"}]}'),
        (200, {}, _raw_payload(_arf_report())),
    ]
    with _server(responses) as (base_url, _scenario):
        connector = GmailConnector(GmailApiHttpTransport(base_url))
        await connector.configure(_Secrets())
        page = await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.kind.value == "complaint"
    assert item.ordinal == 0
    assert item.occurred_at == datetime(2026, 8, 13, 10, tzinfo=UTC)
    digest = hashlib.sha256(b"m1").hexdigest()
    assert item.provider_ref_digest == digest
    assert item.provider_event_id == hashlib.sha256(
        f"{digest}:0".encode("ascii")
    ).hexdigest()
    assert item.correlation.route_id == "route-v1"
    assert (
        item.correlation.deterministic_message_id
        == "route-v1." + "a" * 64 + "@messages.tradeos.invalid"
    )
    assert item.correlation.idempotency_header == "route-v1." + "b" * 64


@pytest.mark.asyncio
async def test_plain_mail_mentioning_complaint_produces_no_feedback_facts() -> None:
    responses = [
        (200, {}, b'{"historyId":"100"}'),
        (200, {}, b'{"messages":[{"id":"m2"}]}'),
        (200, {}, _raw_payload(b"Subject: abuse complaint\r\n\r\nstop mailing us\r\n")),
    ]
    with _server(responses) as (base_url, _scenario):
        connector = GmailConnector(GmailApiHttpTransport(base_url))
        await connector.configure(_Secrets())
        page = await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert page.items == ()


@pytest.mark.asyncio
async def test_bootstrap_cursor_freezes_cutoff_and_splits_multi_recipient_blocks() -> None:
    responses = [
        (200, {}, b'{"historyId":"100"}'),
        (
            200,
            {},
            b'{"messages":[{"id":"m1"},{"id":"m1"}],"nextPageToken":"p2"}',
        ),
        (200, {}, _raw_payload(_dsn(("5.1.1", "4.2.2")))),
        (200, {}, _raw_payload(_dsn(("5.1.1", "4.2.2")))),
        (200, {}, b'{"messages":[{"id":"m2"}]}'),
        (200, {}, _raw_payload(b"Subject: ordinary\r\n\r\nStatus: 5.1.1")),
        (
            200,
            {},
            (
                b'{"history":[{"messagesAdded":[{"message":{"id":"m3"}},'
                b'{"message":{"id":"m3"}}]}],"historyId":"102"}'
            ),
        ),
        (200, {}, _raw_payload(_dsn(("5.1.1",)))),
    ]
    with _server(responses) as (base_url, scenario):
        connector = GmailConnector(
            GmailApiHttpTransport(base_url, timeout_seconds=30),
            now=lambda: datetime(2026, 8, 13, 10, tzinfo=UTC),
        )
        await connector.configure(_Secrets())
        first = await connector.fetch_feedback_page("feedback-primary", None, 1)
        second = await connector.fetch_feedback_page(
            "feedback-primary", first.next_cursor, 1
        )
        third = await connector.fetch_feedback_page(
            "feedback-primary", second.next_cursor, 1
        )
        fourth = await connector.fetch_feedback_page(
            "feedback-primary", third.next_cursor, 1
        )

    assert [item.ordinal for item in first.items] == [0]
    assert [item.ordinal for item in second.items] == [1]
    assert third.items == ()
    assert [item.kind.value for item in fourth.items] == ["hard_bounce"]
    message_lists = [
        query for path, query, _auth in scenario.requests if path.endswith("/messages")
    ]
    assert len(message_lists) == 2
    assert message_lists[0]["q"] == message_lists[1]["q"]
    assert message_lists[1]["pageToken"] == ["p2"]
    assert all(auth == "Bearer oauth-private-marker" for _, _, auth in scenario.requests)
    assert scenario.responses == []


@pytest.mark.parametrize(
    ("status", "headers", "category", "retry_after"),
    [
        (401, {}, ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
        (403, {}, ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
        (404, {}, ToolErrorCategory.PROVIDER_PERMANENT, None),
        (429, {"Retry-After": "17"}, ToolErrorCategory.RATE_LIMITED, 17),
        (429, {"Retry-After": "3600"}, ToolErrorCategory.RATE_LIMITED, 3600),
        (429, {"Retry-After": "3601"}, ToolErrorCategory.RATE_LIMITED, None),
        (429, {"Retry-After": "9999"}, ToolErrorCategory.RATE_LIMITED, None),
        (429, {"Retry-After": "0"}, ToolErrorCategory.RATE_LIMITED, None),
        (429, {"Retry-After": "invalid"}, ToolErrorCategory.RATE_LIMITED, None),
        (500, {}, ToolErrorCategory.PROVIDER_TRANSIENT, None),
        (302, {"Location": "https://example.com/private"}, ToolErrorCategory.PROVIDER_TRANSIENT, None),
    ],
)
@pytest.mark.asyncio
async def test_feedback_http_errors_are_typed_safe_and_redirects_rejected(
    status: int,
    headers: dict[str, str],
    category: ToolErrorCategory,
    retry_after: int | None,
) -> None:
    with _server([(status, headers, b"private error body")]) as (base_url, _scenario):
        connector = GmailConnector(GmailApiHttpTransport(base_url))
        await connector.configure(_Secrets())
        with pytest.raises(ToolGatewayError) as raised:
            await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert raised.value.category is category
    assert raised.value.retry_after_seconds == retry_after
    for forbidden in ("oauth-private-marker", "private error body", base_url):
        assert forbidden not in str(raised.value)


@pytest.mark.parametrize("timeout", (0, 30.1, 31, True))
def test_feedback_transport_timeout_is_at_most_exactly_30_seconds(
    timeout: object,
) -> None:
    with pytest.raises(ValidationError):
        GmailApiHttpTransport(timeout_seconds=timeout)  # type: ignore[arg-type]


class _NetworkFailureTransport:
    async def search(self, *, token: str, message_id: str, header: str) -> None:
        del token, message_id, header

    async def send(self, *, token: str, raw_message: bytes) -> str:
        del token, raw_message
        return "unused"

    async def get_profile_history_id(self, *, token: str) -> str:
        del token
        raise GmailNetworkError(may_have_written=False)

    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]:
        del token, after_epoch, page_token
        raise AssertionError("profile failure must stop")

    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]:
        del token, start_history_id, page_token
        raise AssertionError("profile failure must stop")

    async def get_raw_message(self, *, token: str, message_ref: str) -> bytes:
        del token, message_ref
        raise AssertionError("profile failure must stop")


@pytest.mark.asyncio
async def test_feedback_network_failure_is_typed_provider_transient() -> None:
    connector = GmailConnector(_NetworkFailureTransport())
    await connector.configure(_Secrets())
    with pytest.raises(ToolGatewayError) as raised:
        await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert raised.value.category is ToolErrorCategory.PROVIDER_TRANSIENT
    assert "oauth-private-marker" not in str(raised.value)


@pytest.mark.asyncio
async def test_malformed_or_oversized_provider_payload_fails_without_raw_echo() -> None:
    payloads = (b"not-json-private-marker", b"x" * (4 * 1024 * 1024 + 1))
    for payload in payloads:
        with _server([(200, {}, payload)]) as (base_url, _scenario):
            connector = GmailConnector(GmailApiHttpTransport(base_url))
            await connector.configure(_Secrets())
            with pytest.raises(ToolGatewayError) as raised:
                await connector.fetch_feedback_page("feedback-primary", None, 10)
        assert raised.value.category is ToolErrorCategory.PROVIDER_TRANSIENT
        assert "private-marker" not in str(raised.value)


@pytest.mark.asyncio
async def test_malformed_or_oversized_raw_message_is_typed_transient() -> None:
    raw_payloads = (
        b'{"raw":"***private-raw-marker***"}',
        _raw_payload(b"x" * (4 * 1024 * 1024 + 1)),
    )
    for raw_payload in raw_payloads:
        responses = [
            (200, {}, b'{"historyId":"100"}'),
            (200, {}, b'{"messages":[{"id":"message-safe-1"}]}'),
            (200, {}, raw_payload),
        ]
        with _server(responses) as (base_url, _scenario):
            connector = GmailConnector(GmailApiHttpTransport(base_url))
            await connector.configure(_Secrets())
            with pytest.raises(ToolGatewayError) as raised:
                await connector.fetch_feedback_page("feedback-primary", None, 10)
        assert raised.value.category is ToolErrorCategory.PROVIDER_TRANSIENT
        assert "private-raw-marker" not in str(raised.value)


@pytest.mark.asyncio
async def test_raw_message_response_allows_base64_expansion_up_to_mime_cap() -> None:
    raw = b"x" * (3_200 * 1024)
    with _server([(200, {}, _raw_payload(raw))]) as (base_url, scenario):
        transport = GmailApiHttpTransport(base_url)
        received = await transport.get_raw_message(
            token="oauth-private-marker",
            message_ref="message-safe-1",
        )
    assert received == raw
    assert scenario.responses == []


@pytest.mark.asyncio
async def test_stale_history_cursor_404_is_permanent_without_silent_bootstrap() -> None:
    responses = [
        (200, {}, b'{"historyId":"100"}'),
        (200, {}, b'{"messages":[]}'),
        (404, {}, b"private stale history marker"),
    ]
    with _server(responses) as (base_url, scenario):
        connector = GmailConnector(GmailApiHttpTransport(base_url))
        await connector.configure(_Secrets())
        bootstrap = await connector.fetch_feedback_page(
            "feedback-primary", None, 10
        )
        with pytest.raises(ToolGatewayError) as raised:
            await connector.fetch_feedback_page(
                "feedback-primary", bootstrap.next_cursor, 10
            )
    assert raised.value.category is ToolErrorCategory.PROVIDER_PERMANENT
    assert len(scenario.requests) == 3
    assert scenario.responses == []


@pytest.mark.asyncio
async def test_history_page_over_100_message_refs_fails_before_cursor_is_returned() -> None:
    added = [{"message": {"id": f"message-{index}"}} for index in range(102)]
    history = json.dumps(
        {
            "history": [{"messagesAdded": added}],
            "historyId": "101",
        },
        separators=(",", ":"),
    ).encode("ascii")
    responses = [
        (200, {}, b'{"historyId":"100"}'),
        (200, {}, b'{"messages":[]}'),
        (200, {}, history),
        (200, {}, _raw_payload(_dsn(("5.1.1",)))),
    ]
    with _server(responses) as (base_url, _scenario):
        connector = GmailConnector(GmailApiHttpTransport(base_url))
        await connector.configure(_Secrets())
        bootstrap = await connector.fetch_feedback_page(
            "feedback-primary", None, 1
        )
        with pytest.raises(ToolGatewayError) as raised:
            await connector.fetch_feedback_page(
                "feedback-primary", bootstrap.next_cursor, 1
            )
    assert raised.value.category is ToolErrorCategory.PROVIDER_TRANSIENT


@pytest.mark.asyncio
async def test_history_duplicate_refs_are_deduplicated_before_unique_cap() -> None:
    added = [{"message": {"id": "message-one"}} for _ in range(101)]
    history = json.dumps(
        {
            "history": [{"messagesAdded": added}],
            "historyId": "101",
        },
        separators=(",", ":"),
    ).encode("ascii")
    responses = [
        (200, {}, b'{"historyId":"100"}'),
        (200, {}, b'{"messages":[]}'),
        (200, {}, history),
        (200, {}, _raw_payload(_dsn(("5.1.1",)))),
    ]
    with _server(responses) as (base_url, scenario):
        connector = GmailConnector(GmailApiHttpTransport(base_url))
        await connector.configure(_Secrets())
        bootstrap = await connector.fetch_feedback_page(
            "feedback-primary", None, 10
        )
        page = await connector.fetch_feedback_page(
            "feedback-primary", bootstrap.next_cursor, 10
        )
    assert [item.kind.value for item in page.items] == ["hard_bounce"]
    raw_requests = [
        path for path, _, _ in scenario.requests if path.endswith("/message-one")
    ]
    assert len(raw_requests) == 1


@pytest.mark.asyncio
async def test_cursor_is_opaque_bounded_and_caller_cannot_inject_query() -> None:
    connector = GmailConnector(GmailApiHttpTransport("https://gmail.googleapis.com"))
    await connector.configure(_Secrets())
    for cursor in ("not-base64", "Bearer-private", "x" * 32769):
        with pytest.raises(ValidationError) as raised:
            await connector.fetch_feedback_page("feedback-primary", cursor, 10)
        assert cursor not in str(raised.value)
    for limit in (0, 101, True):
        with pytest.raises(ValidationError):
            await connector.fetch_feedback_page("feedback-primary", None, limit)


class _RawInjectingTransport:
    """绕过 transport 4MiB 上限的测试 transport，直接把构造好的 raw 交给解析。"""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def search(self, *, token: str, message_id: str, header: str) -> None:
        del token, message_id, header

    async def send(self, *, token: str, raw_message: bytes) -> str:
        del token, raw_message
        raise AssertionError("feedback 路径不得发送")

    async def get_profile_history_id(self, *, token: str) -> str:
        del token
        return "100"

    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]:
        del token, after_epoch, page_token
        return (("m1",), None)

    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]:
        del token, start_history_id, page_token
        return ((), None, "100")

    async def get_raw_message(self, *, token: str, message_ref: str) -> bytes:
        del token, message_ref
        return self._raw


@pytest.mark.asyncio
async def test_oversized_structured_feedback_report_is_typed_quarantine_not_dsn_error() -> None:
    """超大 structured feedback-report 必须 typed MALFORMED 供隔离，不能先被
    DSN parser 抛 ValidationError。"""
    oversized = _arf_report(report_pad=4 * 1024 * 1024)
    connector = GmailConnector(_RawInjectingTransport(oversized))
    await connector.configure(_Secrets())
    page = await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.kind.value == "unparseable"
    assert item.parse_issue.value == "malformed"
    assert item.correlation is None


@pytest.mark.asyncio
async def test_oversized_ordinary_mail_never_becomes_complaint_or_dsn_error() -> None:
    """超大普通邮件不得误判成 complaint，也不得被 DSN parser 拒绝。"""
    oversized = b"Subject: ordinary\r\n\r\n" + b"x" * (4 * 1024 * 1024 + 1)
    connector = GmailConnector(_RawInjectingTransport(oversized))
    await connector.configure(_Secrets())
    page = await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert page.items == ()


@pytest.mark.asyncio
async def test_arf_without_date_is_typed_malformed_not_complaint() -> None:
    """缺失 Date 的 ARF 必须 typed MALFORMED 供隔离，不能以 EPOCH 形成 COMPLAINT。"""
    connector = GmailConnector(
        _RawInjectingTransport(_arf_report(date_header=None))
    )
    await connector.configure(_Secrets())
    page = await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.kind.value == "unparseable"
    assert item.parse_issue.value == "malformed"
    assert item.correlation is None
    assert item.occurred_at == datetime(1970, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_folded_content_type_report_type_is_not_missed() -> None:
    """RFC continuation folding 的 Content-Type（report-type 在续行）不得漏判。"""
    connector = GmailConnector(
        _RawInjectingTransport(_arf_report(folded_content_type=True))
    )
    await connector.configure(_Secrets())
    page = await connector.fetch_feedback_page("feedback-primary", None, 10)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.kind.value == "complaint"
    assert item.parse_issue is None
