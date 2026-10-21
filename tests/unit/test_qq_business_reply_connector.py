"""受控协议替身检验普通回复传输；绝不访问真实邮箱。"""

from dataclasses import replace
from email.parser import BytesParser
from email.policy import default

import pytest

from connectors.qq_mail.reply_client import QQBusinessReplyConnector
from shared.errors import ValidationError
from shared.schemas.business_reply import SMTPReplyRequest
from tests.unit.test_aliyun_mail_connector import PASSWORD, MailServer
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError


def request() -> SMTPReplyRequest:
    return SMTPReplyRequest(
        "sales@qq.com",
        "buyer@example.org",
        "Re: Enquiry",
        "Hello,\nPlease confirm the specification.",
        "tradeos-reply@qq.com",
        "reply-1",
        "<Original@EXAMPLE.org>",
        ("<older@example.org>", "<Original@EXAMPLE.org>"),
    )


def connector(server: MailServer) -> QQBusinessReplyConnector:
    return QQBusinessReplyConnector("sales@qq.com", PASSWORD, transport=server)


@pytest.mark.asyncio
async def test_real_mime_has_thread_and_hash_without_marketing_headers_and_deduplicates() -> (
    None
):
    server = MailServer()
    value = request()
    first = await connector(server).send_once(value)
    message = BytesParser(policy=default).parsebytes(server.sent[0])
    assert str(message["From"]) == "sales@qq.com"
    assert str(message["To"]) == "buyer@example.org"
    assert str(message["Subject"]) == "Re: Enquiry"
    assert str(message["Message-ID"]) == "<tradeos-reply@qq.com>"
    assert str(message["X-TradeOS-Idempotency-V1"]) == "reply-1"
    assert str(message["In-Reply-To"]) == "<Original@EXAMPLE.org>"
    assert str(message["References"]) == "<older@example.org> <Original@EXAMPLE.org>"
    assert str(message["X-TradeOS-Content-SHA256"]) == value.content_hash
    assert not any(key.lower().startswith("list-") for key in message)
    assert message.get_content().replace("\r\n", "\n").rstrip() == value.body
    assert server.saved == server.sent
    server.search_ids, server.headers = b"91", server.saved[0]
    second = await connector(server).send_once(value)
    assert second.already_existed and second.provider_ref == first.provider_ref
    assert second.certainty is DeliveryCertainty.SENT and len(server.sent) == 1


@pytest.mark.parametrize("kind", ["body", "thread", "duplicate"])
@pytest.mark.asyncio
async def test_sent_candidate_must_match_body_hash_and_real_thread(kind: str) -> None:
    server = MailServer()
    await connector(server).send_once(request())
    server.search_ids, server.headers = b"91", server.saved[0]
    server.sent.clear()
    value = request()
    if kind == "body":
        value = replace(value, body="Other body")
    elif kind == "thread":
        server.headers = server.headers.replace(
            b"<Original@EXAMPLE.org>", b"<different@example.org>"
        )
    else:
        server.headers = b"In-Reply-To: <Original@EXAMPLE.org>\r\n" + server.headers
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(value)
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert server.sent == []


@pytest.mark.asyncio
async def test_reconcile_never_sends_when_missing() -> None:
    server = MailServer()
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).reconcile_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert ("smtp",) not in server.calls


@pytest.mark.asyncio
async def test_data_timeout_unknown_and_append_failure_keeps_sent() -> None:
    server = MailServer()
    server.smtp_error = TimeoutError("sensitive response")
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert len(server.sent) == 1 and not server.saved
    server = MailServer()
    server.append_error = True
    assert (
        await connector(server).send_once(request())
    ).certainty is DeliveryCertainty.SENT


@pytest.mark.asyncio
async def test_wrong_sender_and_cold_request_fail_before_io() -> None:
    from tests.unit.test_aliyun_mail_connector import request as cold_request

    server = MailServer()
    for value in (replace(request(), from_address="other@qq.com"), cold_request()):
        with pytest.raises(ValidationError):
            await connector(server).send_once(value)
    assert server.calls == []


@pytest.mark.parametrize(
    "kind", ["references", "hash", "subject", "recipient", "defect", "too_many"]
)
@pytest.mark.asyncio
async def test_conflicting_or_unbounded_sent_evidence_never_sends(kind: str) -> None:
    server = MailServer()
    await connector(server).send_once(request())
    server.search_ids, server.headers = b"91", server.saved[0]
    server.sent.clear()
    if kind == "references":
        server.headers = server.headers.replace(
            b"<older@example.org>", b"<other@example.org>"
        )
    elif kind == "hash":
        server.headers = server.headers.replace(
            request().content_hash.encode(), b"0" * 64
        )
    elif kind == "subject":
        server.headers = server.headers.replace(b"Re: Enquiry", b"Different")
    elif kind == "recipient":
        server.headers = server.headers.replace(
            b"buyer@example.org", b"other@example.org"
        )
    elif kind == "defect":
        server.headers = server.headers.replace(
            b"From: sales@qq.com", b"From: <sales@qq.com"
        )
    else:
        server.search_ids = b" ".join(str(i).encode() for i in range(1, 22))
    with pytest.raises(ToolGatewayError) as caught:
        await connector(server).send_once(request())
    assert caught.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert server.sent == []
