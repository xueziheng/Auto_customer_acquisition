"""单封追问的范围、原件关联、测试证据与SMTP未知结果边界。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import default

import pytest

from connectors.qq_mail.business_reply import QQBusinessReplyConnector, QQReplyEnvelope
from connectors.qq_mail.client import QQMailConnector
from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.business_reply import (
    BODY,
    BusinessReplyScope,
    eligible_reply,
    source_evidence,
    validate_source,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import new_id
from tests.unit.test_aliyun_mail_connector import PASSWORD, MailServer
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError

ORIGINAL = "This is an internal workflow test, not a real purchase order.\nI am interested in the YJ-168 solar pedestal fan. Please send the specifications and minimum order quantity."


def scope():
    now = datetime.now(UTC)
    return BusinessReplyScope(
        enabled=True,
        actor=BusinessMailActor(
            tenant_id=new_id("tn"), employee_id=new_id("emp"), user_id=new_id("usr")
        ),
        message_id=new_id("bmi"),
        source_sha256="a" * 64,
        sender="1249583590@qq.com",
        recipient="sales@jslingte.com",
        binding_operation_id="aco-current",
        identity_id=new_id("sid"),
        identity_version=2,
        original_message_id="<inquiry@jslingte.com>",
        approved_at=now,
        expires_at=now + timedelta(hours=2),
        max_sends=1,
        purpose="internal_workflow_test",
        approval_source="受控测试fixture",
    )


def envelope():
    return QQReplyEnvelope(
        "1249583590@qq.com",
        "sales@jslingte.com",
        "Re: [TEST-04] YJ-168 purchase inquiry",
        BODY,
        "businessreply.fixture@messages.tradeos.invalid",
        "businessreply.fixture",
        "<inquiry@jslingte.com>",
    )


def test_scope_and_source_cannot_expand_to_real_or_multiple_send():
    s = scope()
    data = dict(  # noqa: C408 -- 保持fixture字段可读
        sender=s.recipient,
        subject="[TEST-04] YJ-168 purchase inquiry",
        body=ORIGINAL,
        header=s.original_message_id,
        source_sha256=s.source_sha256,
        source_kind="full_mime",
        category="business",
        status="classified",
    )
    validate_source(s, **data)
    for change in (
        {"category": "other"},
        {"body": "I need 100 units."},
        {"source_sha256": "b" * 64},
        {"header": "<wrong@x.test>"},
    ):
        with pytest.raises(ValidationError):
            validate_source(s, **(data | change))
    for change in (
        {"max_sends": 2},
        {"purpose": "customer"},
        {"original_message_id": "<x@y>\r\nBcc: z@y.com"},
    ):
        with pytest.raises(ValueError):
            BusinessReplyScope.model_validate(s.model_dump() | change)


def test_thread_and_auto_response_boundaries():
    s = scope()
    h = {
        "From": s.recipient,
        "To": s.sender,
        "Message-ID": "<reply@jslingte.com>",
        "In-Reply-To": s.original_message_id,
    }
    assert eligible_reply(s, h, "<out@tradeos.invalid>")
    for change in (
        {"From": "other@example.com"},
        {"To": "other@qq.com"},
        {"In-Reply-To": "<unrelated@x.com>"},
        {"Auto-Submitted": "auto-replied"},
        {"Precedence": "bulk"},
        {"Message-ID": s.original_message_id},
    ):
        assert not eligible_reply(s, h | change, "<out@tradeos.invalid>")


def test_test_evidence_retains_quotes_never_promotes_or_reads_quoted_text():
    body = "Application: classroom test only\nQuantity: 12 test units\nOn yesterday someone wrote:\nProduct: fabricated"
    result = source_evidence(
        message_id="source", artifact_id="artifact", source_sha256="a" * 64, body=body
    )
    assert result["fields"]["quantity"]["evidence_quote"] == "Quantity: 12 test units"
    assert result["fields"]["quantity"]["confirmed_by"] is None
    assert "product" not in result["fields"]
    assert result["validated_need"] is result["opportunity"] is False


async def test_connector_preserves_thread_and_suppresses_auto_loops():
    server = MailServer()
    conn = QQBusinessReplyConnector(
        QQMailConnector(envelope().from_address, PASSWORD, transport=server)
    )
    result = await conn.send_once(envelope())
    assert result.certainty is DeliveryCertainty.SENT
    parsed = BytesParser(policy=default).parsebytes(server.sent[0])
    assert parsed["In-Reply-To"] == parsed["References"] == envelope().in_reply_to
    assert parsed["Auto-Submitted"] == "auto-replied"
    assert parsed["List-Unsubscribe"] is None
    assert server.saved == server.sent
    server.search_ids = b"91"
    server.headers = server.sent[0].split(b"\r\n\r\n")[0] + b"\r\n\r\n"
    assert (await conn.send_once(envelope())).already_existed
    assert len(server.sent) == 1


async def test_connector_uncertain_never_resends_on_reconciliation():
    server = MailServer()
    server.smtp_error = TimeoutError("private-provider-text")
    conn = QQBusinessReplyConnector(
        QQMailConnector(envelope().from_address, PASSWORD, transport=server)
    )
    with pytest.raises(ToolGatewayError) as failure:
        await conn.send_once(envelope())
    assert failure.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    with pytest.raises(ToolGatewayError):
        await conn.reconcile_once(envelope())
    assert len(server.sent) == 1 and not server.saved


@pytest.mark.parametrize(
    "change",
    [{"in_reply_to": "<x@y>\nBcc: other@y"}, {"recipient_address": "a@y.com,b@y.com"}],
)
def test_header_injection_rejected(change):
    with pytest.raises(ValidationError):
        replace(envelope(), **change)


@pytest.mark.parametrize("separator", ["\n", " "])
def test_mobile_reply_literals_keep_exact_values_and_ignore_signature(separator):
    body = (
        separator.join(
            [
                "This is an internal workflow test, not a real purchase order.",
                "Product: YJ-168 solar pedestal fan",
                "Application: Indoor ventilation (test data)",
                "Quantity: 100 units (test data)",
            ]
        )
        + "\n来自阿里邮箱Android客户端---\nQuantity: 999 real units"
    )
    result = source_evidence(
        message_id="source", artifact_id="artifact", source_sha256="a" * 64, body=body
    )
    assert result["missing_fields"] == []
    assert result["fields"]["product"]["value"] == "YJ-168 solar pedestal fan"
    assert result["fields"]["application"]["value"] == "Indoor ventilation (test data)"
    assert result["fields"]["quantity"]["value"] == "100 units (test data)"
    for field in result["fields"].values():
        assert field["evidence_quote"] in body
        assert field["confirmed_by"] is field["confirmed_at"] is None
    assert not result["validated_need"] and not result["opportunity"]


def test_incomplete_mobile_reply_does_not_invent_missing_values():
    body = "This is an internal workflow test. Product: YJ-168 solar pedestal fan Application"
    result = source_evidence(
        message_id="source", artifact_id="artifact", source_sha256="a" * 64, body=body
    )
    assert result["missing_fields"] == ["application", "quantity"]
    assert "quantity" not in result["fields"]
