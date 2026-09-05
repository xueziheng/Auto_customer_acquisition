"""入站技术候选，不以header匹配冒充已验证关联。"""

from datetime import UTC, datetime, timedelta

import pytest

from shared.schemas.identifiers import SendingIdentityId, TenantId, new_id


def test_typed_inbound_is_separate_from_feedback() -> None:
    from connectors.gmail.inbound import GmailInboundReader
    from connectors.gmail.inbound_cursor import decode_cursor, initial_inbound_cursor
    from shared.schemas.email_inbound import InboundRoute

    route = InboundRoute(
        tenant_id=TenantId(new_id("tn")),
        mailbox_alias="primary",
        configured_identity_id=SendingIdentityId(new_id("sid")),
        route_id="controlled",
        config_version="v1",
    )
    now = datetime.now(UTC)
    cursor = initial_inbound_cursor(
        route, now, int((now - timedelta(days=30)).timestamp())
    )
    assert cursor.startswith("gic1.")
    assert decode_cursor(cursor, route).phase == "initial"
    assert GmailInboundReader.__name__ == "GmailInboundReader"


from connectors.gmail.inbound_mime import parse_inbound_message
from connectors.gmail.inbound_transport import (
    GmailInboundApiTransport,
    GmailInboundRawResult,
)
from shared.schemas.email_inbound import InboundDisposition as D

DATE = datetime(2026, 9, 5, 9, tzinfo=UTC)
REPLY = "<controlled." + "a" * 64 + "@messages.tradeos.invalid>"


def mime(
    *,
    headers="",
    body="We need hinges.",
    message_id="<buyer@example.invalid>",
    reply=REPLY,
    date="Sat, 05 Sep 2026 09:00:00 +0000",
):
    lines = []
    if message_id is not None:
        lines.append("Message-ID: " + message_id)
    if reply is not None:
        lines.append("In-Reply-To: " + reply)
    if date is not None:
        lines.append("Date: " + date)
    lines += ["Subject: Internal fixture", "From: buyer@example.invalid"]
    if headers:
        lines.append(headers)
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


def parse(raw, labels=("INBOX",)):
    return parse_inbound_message(
        "provider-fixture",
        GmailInboundRawResult(
            status="raw", raw_mime=raw, labels=labels, internal_date=DATE
        ),
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, D.CANDIDATE),
        ({"headers": "Auto-Submitted: auto-replied", "body": "I am away"}, D.CANDIDATE),
        ({"body": "Please unsubscribe me"}, D.CANDIDATE),
        ({"message_id": None}, D.MISSING_MESSAGE_ID),
        ({"headers": "Message-ID: <other@example.invalid>"}, D.INVALID_MESSAGE_ID),
        ({"message_id": "<buyer@example.invalid> trailing"}, D.INVALID_MESSAGE_ID),
        (
            {"message_id": "<buyer@example.invalid> <other@example.invalid>"},
            D.INVALID_MESSAGE_ID,
        ),
        ({"reply": None, "headers": "References: " + REPLY}, D.INVALID_IN_REPLY_TO),
        ({"reply": REPLY + " " + REPLY}, D.INVALID_IN_REPLY_TO),
        ({"headers": "In-Reply-To: " + REPLY}, D.INVALID_IN_REPLY_TO),
        ({"date": None}, D.INVALID_SENT_AT),
        ({"date": "Sat, 05 Sep 2026 09:00:00"}, D.INVALID_SENT_AT),
        ({"date": "Sat, 05 Sep 2026 09:00:00 -0000"}, D.INVALID_SENT_AT),
        ({"headers": "Date: Sat, 05 Sep 2026 09:00:00 +0000"}, D.INVALID_SENT_AT),
        ({"body": ""}, D.NO_BODY),
        (
            {"headers": "Content-Type: multipart/report; report-type=delivery-status"},
            D.SKIPPED_DELIVERY_REPORT,
        ),
        (
            {"headers": "Content-Type: multipart/report; report-type=feedback-report"},
            D.SKIPPED_DELIVERY_REPORT,
        ),
        (
            {"headers": "Content-Type: multipart/report; report-type=unknown"},
            D.SKIPPED_DELIVERY_REPORT,
        ),
        ({"headers": "Content-Type: message/rfc822"}, D.NO_BODY),
        ({"headers": "Content-Disposition: attachment"}, D.NO_BODY),
        (
            {"headers": "Content-Transfer-Encoding: base64", "body": "a!invalid"},
            D.MALFORMED,
        ),
        ({"headers": "Content-Type: text/plain; charset=no-such-charset"}, D.MALFORMED),
        ({"headers": "X-Large: " + "a" * 65536}, D.HEADER_LIMIT),
    ],
)
def test_mime_disposition_matrix(kwargs, expected):
    assert parse(mime(**kwargs)).disposition == expected


@pytest.mark.parametrize("label", ["SENT", "DRAFT", "SPAM", "TRASH"])
def test_labels_are_provider_facts(label):
    assert parse(mime(), (label,)).disposition == D.SKIPPED_LABEL


def test_sensitive_repr_serialization_and_exact_case():
    raw = mime(message_id="<Buyer@Example.invalid>")
    item = parse(raw)
    assert item.external_message_id == "<Buyer@Example.invalid>"
    assert item.in_reply_to == REPLY
    for rendered in (repr(item), item.model_dump_json()):
        for forbidden in ("Buyer@", REPLY, "We need", "Internal fixture"):
            assert forbidden not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid",
        "https://gmail.googleapis.com/path",
        "http://127.0.0.1:12345",
        "http://localhost",
        "http://user@localhost:12345",
    ],
)
def test_transport_endpoint_closed_by_default(url):
    from shared.schemas.email_inbound import InboundError

    with pytest.raises(InboundError):
        GmailInboundApiTransport(url)


def test_part_and_depth_limits_before_tree_construction():
    parts = ("--b\r\nContent-Type: text/plain\r\n\r\nx\r\n" * 100) + "--b--\r\n"
    assert (
        parse(
            mime(headers="Content-Type: multipart/mixed; boundary=b", body=parts)
        ).disposition
        == D.MIME_LIMIT
    )
    payload = "Content-Type: text/plain\r\n\r\nx"
    for i in range(21):
        boundary = f"b{i}"
        payload = f"Content-Type: multipart/mixed; boundary={boundary}\r\n\r\n--{boundary}\r\n{payload}\r\n--{boundary}--"
    assert (
        parse(
            mime(
                headers=payload.split("\r\n\r\n")[0],
                body=payload.split("\r\n\r\n", 1)[1],
            )
        ).disposition
        == D.MIME_LIMIT
    )


def test_html_is_textual_but_guard_candidate_keeps_hidden_marker():
    item = parse(
        mime(
            headers="Content-Type: text/html",
            body="<p>Hello</p><script>cookie</script>",
        )
    )
    assert item.body.strip() == "Hello"
    assert "cookie" in item.guard_body


def test_cursor_bounds_route_binding_and_fixed_start():
    from connectors.gmail.inbound_cursor import decode_cursor, initial_inbound_cursor
    from shared.schemas.email_inbound import InboundError, InboundRoute

    route = InboundRoute(
        tenant_id=TenantId(new_id("tn")),
        mailbox_alias="primary",
        configured_identity_id=SendingIdentityId(new_id("sid")),
        route_id="controlled",
        config_version="v1",
    )
    after = int((DATE - timedelta(days=30)).timestamp())
    valid = initial_inbound_cursor(route, DATE, after)
    for cursor in ("gfc1.aaa", "gic1." + "a" * 32768, "gic1.!bad", valid + "="):
        with pytest.raises(InboundError):
            decode_cursor(cursor, route)
    for field, value in [
        ("tenant_id", TenantId(new_id("tn"))),
        ("mailbox_alias", "other"),
        ("configured_identity_id", SendingIdentityId(new_id("sid"))),
        ("config_version", "v2"),
    ]:
        with pytest.raises(InboundError):
            decode_cursor(valid, route.model_copy(update={field: value}))
    with pytest.raises(InboundError):
        initial_inbound_cursor(route, DATE, after - 1)
    assert decode_cursor(valid, route).after_epoch == after


def test_decode_expansion_rejected_during_candidate_decode():
    raw = mime(headers="Content-Type: text/plain; charset=iso-8859-1", body="x")[
        :-1
    ] + b"\xff" * (3 * 1024 * 1024)
    assert parse(raw).disposition == D.MIME_LIMIT


@pytest.mark.parametrize(
    "invalid_id",
    [
        "<buyer..name@example.invalid>",
        "<.buyer@example.invalid>",
        "<buyer@example..invalid>",
        "<buyer@.example.invalid>",
    ],
)
def test_message_id_requires_dot_atom_syntax(invalid_id):
    assert parse(mime(message_id=invalid_id)).disposition is D.INVALID_MESSAGE_ID


def test_date_timezone_cannot_be_followed_by_garbage():
    assert (
        parse(mime(date="Sat, 05 Sep 2026 09:00:00 +0000 garbage")).disposition
        is D.INVALID_SENT_AT
    )


def test_attached_multipart_is_not_customer_original_text():
    body = "--b\r\nContent-Type: text/plain\r\n\r\nforwarded\r\n--b--"
    item = parse(
        mime(
            headers="Content-Type: multipart/mixed; boundary=b\r\nContent-Disposition: attachment",
            body=body,
        )
    )
    assert item.disposition is D.NO_BODY


@pytest.mark.asyncio
async def test_slot_replayed_success_and_binding_mismatches_fail_closed():
    import asyncio

    from connectors.gmail.inbound_cursor import initial_inbound_cursor
    from shared.schemas.email_inbound import (
        ArchivedInboundPage,
        InboundError,
        InboundRoute,
    )
    from shared.schemas.identifiers import UserId
    from tool_gateway.errors import ToolCallStatus, ToolGatewayError
    from tool_gateway.handlers.email_inbound import ToolGatewayEmailInboundReader
    from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
    from tool_gateway.pipeline import ToolCallResult

    route = InboundRoute(
        tenant_id=TenantId(new_id("tn")),
        mailbox_alias="primary",
        configured_identity_id=SendingIdentityId(new_id("sid")),
        route_id="controlled",
        config_version="v1",
    )
    cursor = initial_inbound_cursor(route, DATE, int(DATE.timestamp()) - 86400)
    valid = ArchivedInboundPage(
        route=route, starting_cursor=cursor, next_cursor=cursor, items=()
    )
    slot = InboundPageSlot()

    # 只测试进程内领取边界的伪造返回值；真实Gateway成功/拒绝/故障另由PG集成测试覆盖。
    class ResultBoundary:
        page = valid
        status = ToolCallStatus.SUCCEEDED
        saved = None
        cancel = False

        async def invoke(self, ctx):
            if self.saved is not None:
                return self.saved
            handle = slot.put(self.page)
            if self.cancel:
                raise asyncio.CancelledError()
            self.saved = ToolCallResult(
                tool_call_id=new_id("tcl"),
                tool_id="email.inbound.fetch",
                status=self.status,
                output={"provider_ref": handle},
                duplicate_of=new_id("tcl")
                if self.status is ToolCallStatus.DUPLICATE
                else None,
            )
            return self.saved

    boundary = ResultBoundary()
    reader = ToolGatewayEmailInboundReader(boundary, slot, UserId(new_id("usr")), route)
    assert await reader.fetch(route.tenant_id, "primary", cursor, 20) == valid
    with pytest.raises(InboundError):
        await reader.fetch(route.tenant_id, "primary", cursor, 20)
    for bad in [
        valid.model_copy(update={"starting_cursor": "invalid"}),
        valid.model_copy(
            update={
                "route": route.model_copy(update={"tenant_id": TenantId(new_id("tn"))})
            }
        ),
    ]:
        boundary.saved = None
        boundary.page = bad
        with pytest.raises(InboundError):
            await reader.fetch(route.tenant_id, "primary", cursor, 20)
        assert slot.is_empty
    boundary.page, boundary.saved, boundary.status = (
        valid,
        None,
        ToolCallStatus.DUPLICATE,
    )
    with pytest.raises(ToolGatewayError):
        await reader.fetch(route.tenant_id, "primary", cursor, 20)
    assert slot.is_empty
    boundary.saved, boundary.cancel = None, True
    with pytest.raises(asyncio.CancelledError):
        await reader.fetch(route.tenant_id, "primary", cursor, 20)
    assert slot.is_empty


def test_authorized_raw_content_matches_provider_wrapper_without_fake_facts():
    from connectors.gmail.inbound_mime import parse_inbound_content

    raw = mime(
        headers="Content-Type: text/html",
        body="<p>co<b></b>okie</p><script>hidden</script>",
    )
    standalone = parse_inbound_content(raw)
    provider = parse(raw)
    assert standalone.subject == provider.subject
    assert standalone.body == provider.body
    assert standalone.guard_body == provider.guard_body
    assert standalone.disposition == provider.disposition
    assert standalone.parser_version == provider.parser_version
    assert "cookie" in standalone.body and "hidden" in standalone.guard_body
    assert "cookie" not in repr(standalone) + standalone.model_dump_json()
    assert not hasattr(standalone, "internal_date")
    assert not hasattr(standalone, "external_message_id")
