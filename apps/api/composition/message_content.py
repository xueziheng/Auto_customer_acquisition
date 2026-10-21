"""只解析已通过 Gateway 当前权限与完整性检查的邮件内容；不执行 HTML。"""

from email import policy
from email.parser import BytesParser

from connectors.gmail.inbound_mime import parse_inbound_content
from domains.conversations.schemas import MessageContentView


def parse_message_content(raw: bytes) -> MessageContentView:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain = message.get_body(preferencelist=("plain",)) if message.is_multipart() else (
        message if message.get_content_type() == "text/plain" else None
    )
    part = (
        message.get_body(preferencelist=("html", "plain"))
        if message.is_multipart()
        else message
    )
    body = None
    if part is not None and part.get_content_type() == "text/plain":
        body = part.get_content()
        if not isinstance(body, str):
            body = None
    html_only = plain is None
    if body is None and part is not None and part.get_content_type() == "text/html":
        # QQ HTML 回信也显示安全纯文字；不执行标记、不加载远程图片或附件。
        parsed = parse_inbound_content(part.as_bytes())
        if parsed.disposition == "candidate":
            body = parsed.body
    if body is None and plain is not None:
        fallback = plain.get_content()
        if isinstance(fallback, str):
            body = fallback
    return MessageContentView(
        subject=str(message.get("Subject", ""))[:998],
        sender=str(message.get("From", ""))[:2000],
        recipients=str(message.get("To", ""))[:2000],
        body_text=body[:200000] if body is not None else None,
        html_only=html_only,
        truncated=body is not None and len(body) > 200000,
    )
