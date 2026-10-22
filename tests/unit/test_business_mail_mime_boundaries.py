"""完整MIME原件保持不变，分类正文必须剪枝附件子树；无网络或模型调用。"""

import hashlib
from email.message import EmailMessage

from connectors.qq_mail.business_inbound import parse_business_mime


def envelope() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "sales@example.com"
    message["To"] = "1249583590@qq.com"
    message["Subject"] = "Business inquiry"
    message.set_content("MAIN_BODY Please quote hinges.")
    return message


def parse_and_check_original(message: EmailMessage):
    raw = message.as_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    parsed = parse_business_mime(raw, uid=5, uidvalidity=7)
    assert parsed.raw_mime == raw
    assert hashlib.sha256(parsed.raw_mime).hexdigest() == digest
    assert parsed.uid == 5 and parsed.uidvalidity == 7
    assert parsed.parse_failure is None
    assert "MAIN_BODY" in parsed.body_text
    return parsed


async def test_nested_attached_eml_is_not_classification_body():
    outer = envelope()
    attached = EmailMessage()
    attached.set_content("ATTACHMENT_OUTER_ONLY confidential forwarded text.")
    nested = EmailMessage()
    nested.set_content("ATTACHMENT_NESTED_ONLY forwarded instruction.")
    attached.add_attachment(nested, filename="nested.eml")
    outer.add_attachment(attached, filename="attached.eml")
    parsed = parse_and_check_original(outer)
    assert "ATTACHMENT_" not in parsed.body_text

    from agent_runtime.business_mail import BusinessMailClassifier
    from shared.schemas.model_invocation import (
        InvocationIdentity,
        ModelResponse,
        ModelUsage,
    )

    class CandidateModel:
        def __init__(self):
            self.requests = []

        async def generate(self, identity, request):
            self.requests.append(request)
            return ModelResponse(
                text='{"category":"business","reason":"业务询价","evidence_quote":"quote hinges"}',
                model="fixture",
                usage=ModelUsage(
                    input_tokens=None, cached_input_tokens=None, output_tokens=None
                ),
            )

    model = CandidateModel()
    classifier = BusinessMailClassifier(model, model="fixture", max_output_tokens=512)
    await classifier.classify(
        InvocationIdentity(
            tenant_id="tenant",
            employee_id="employee",
            user_id="user",
            run_id="run",
            capability="business_mail_triage",
            configuration_version="v1",
            sequence=0,
        ),
        subject=parsed.subject,
        body=parsed.body_text,
    )
    assert len(model.requests) == 1
    payload = model.requests[0].payload["untrusted_mail"]
    assert "MAIN_BODY" in payload["body"] and "ATTACHMENT_" not in payload["body"]


def test_attachment_multipart_container_prunes_plain_and_html_children():
    outer = envelope()
    outer.make_mixed()
    container = EmailMessage()
    container.set_content("ATTACHMENT_PLAIN_ONLY confidential bundle.")
    container.add_alternative(
        "<p>ATTACHMENT_HTML_ONLY confidential bundle.</p>", subtype="html"
    )
    container["Content-Disposition"] = 'attachment; filename="bundle.mime"'
    outer.attach(container)
    parsed = parse_and_check_original(outer)
    assert "ATTACHMENT_" not in parsed.body_text


def test_normal_multipart_alternative_and_inline_main_body_remain_readable():
    outer = envelope()
    outer.add_alternative("<p>HTML_ALTERNATIVE_BODY</p>", subtype="html")
    outer.get_payload()[0]["Content-Disposition"] = "inline"
    outer.make_mixed()
    inline = EmailMessage()
    inline.set_content("INLINE_MAIN_BODY Additional business details.")
    inline["Content-Disposition"] = "inline"
    outer.attach(inline)
    parsed = parse_and_check_original(outer)
    assert "INLINE_MAIN_BODY" in parsed.body_text
    assert "HTML_ALTERNATIVE_BODY" not in parsed.body_text
