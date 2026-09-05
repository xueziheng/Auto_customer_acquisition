"""有界MIME技术解析；不关联客户、不分类需求、不展开附件为原话。"""

from __future__ import annotations

import base64
import hashlib
import quopri
import re
from datetime import UTC
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from connectors.gmail.inbound_transport import GmailInboundRawResult
from shared.schemas.email_inbound import (
    HEADER_BYTES,
    MIME_BYTES,
    MIME_DEPTH,
    MIME_PARTS,
    InboundContent,
    ProviderInboundItem,
)
from shared.schemas.email_inbound import (
    InboundDisposition as D,
)

_ATOM = r"[A-Za-z0-9!#$%&' *+/=?^_`{|}~-]+".replace(" ", "")
_ID = re.compile(rf"<{_ATOM}(?:\.{_ATOM})*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*>")
_ZONE = re.compile(r"(?:[+-][0-9]{4}|UT|GMT|[ECMP][SD]T)$")
_REPLY = re.compile(r"<(?:[a-z0-9-]{1,32}\.)?[0-9a-f]{64}@messages\.tradeos\.invalid>")


class _ParseLimit(Exception):
    def __init__(self, disposition: D) -> None:
        self.disposition = disposition
        super().__init__("inbound_mime_rejected")


class _Budget:
    def __init__(self) -> None:
        self.headers = 0
        self.parts = 0
        self.decoded = 0

    def header(self, raw: bytes, depth: int) -> tuple[Message, bytes]:
        if depth > MIME_DEPTH or self.parts >= MIME_PARTS:
            raise _ParseLimit(D.MIME_LIMIT)
        self.parts += 1
        # 只查剩余header预算+分隔符，不先构造无限header对象。
        prefix = raw[: HEADER_BYTES - self.headers + 4]
        match = re.search(b"\r?\n\r?\n", prefix)
        if match is None:
            raise _ParseLimit(D.HEADER_LIMIT if len(raw) > len(prefix) else D.MALFORMED)
        self.headers += match.start()
        if self.headers > HEADER_BYTES:
            raise _ParseLimit(D.HEADER_LIMIT)
        headers = BytesHeaderParser(policy=policy.default).parsebytes(
            raw[: match.end()]
        )
        if headers.defects or any(
            len(headers.get_all(name, [])) > 1
            for name in (
                "Content-Type",
                "Content-Transfer-Encoding",
                "Content-Disposition",
            )
        ):
            raise _ParseLimit(D.MALFORMED)
        return headers, raw[match.end() :]

    def text(self, headers: Message, body: bytes) -> str:
        encoding = str(headers.get("Content-Transfer-Encoding", "7bit")).casefold()
        result = bytearray()
        if encoding == "base64":
            # MIME中只允许ASCII空白；清理仍受单MIME上限约束。
            value = re.sub(rb"[\r\n\t ]", b"", body)
            for start in range(0, len(value), 8192):
                chunk = base64.b64decode(value[start : start + 8192], validate=True)
                if self.decoded + len(result) + len(chunk) > MIME_BYTES:
                    raise _ParseLimit(D.MIME_LIMIT)
                result.extend(chunk)
        elif encoding in {"quoted-printable", "7bit", "8bit", "binary"}:
            # QP和原始编码不扩张，解码前预留完整输入预算。
            if self.decoded + len(body) > MIME_BYTES:
                raise _ParseLimit(D.MIME_LIMIT)
            result.extend(
                quopri.decodestring(body) if encoding == "quoted-printable" else body
            )
        else:
            raise _ParseLimit(D.MALFORMED)
        charset = headers.get_content_charset() or "ascii"
        # UTF-8大小最多4倍原bytes，逐块增量解码并限最终候选总bytes。
        import codecs

        decoder = codecs.getincrementaldecoder(charset)(errors="strict")
        pieces: list[str] = []
        for start in range(0, len(result), 8192):
            value_text = decoder.decode(
                bytes(result[start : start + 8192]), final=False
            )
            self.decoded += len(value_text.encode("utf-8"))
            if self.decoded > MIME_BYTES:
                raise _ParseLimit(D.MIME_LIMIT)
            pieces.append(value_text)
        tail = decoder.decode(b"", final=True)
        self.decoded += len(tail.encode("utf-8"))
        if self.decoded > MIME_BYTES:
            raise _ParseLimit(D.MIME_LIMIT)
        return "".join(pieces) + tail


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div"} and not self.hidden:
            self.values.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.values.append(data)


def _walk(
    headers: Message,
    body: bytes,
    budget: _Budget,
    depth: int,
    text: list[str],
    guard_text: list[str],
    allow_text: bool = True,
) -> None:
    content_type = headers.get_content_type()
    allow_text = allow_text and headers.get_content_disposition() != "attachment"
    if content_type.startswith("multipart/"):
        boundary = headers.get_boundary()
        if (
            boundary is None
            or not 1 <= len(boundary) <= 70
            or "\r" in boundary
            or "\n" in boundary
        ):
            raise _ParseLimit(D.MALFORMED)
        marker = b"--" + boundary.encode("ascii")
        pattern = re.compile(rb"(?m)^" + re.escape(marker) + rb"(--)?[ \t]*\r?$")
        previous_end: int | None = None
        closed = False
        for match in pattern.finditer(body):
            if previous_end is not None:
                raw_part = body[previous_end : match.start()]
                child, payload = budget.header(raw_part, depth + 1)
                _walk(child, payload, budget, depth + 1, text, guard_text, allow_text)
            if match.group(1):
                closed = True
                break
            previous_end = match.end() + (
                1 if body[match.end() : match.end() + 1] == b"\n" else 0
            )
        if not closed or previous_end is None:
            raise _ParseLimit(D.MALFORMED)
    elif content_type in {"text/plain", "text/html"} and allow_text:
        value = budget.text(headers, body)
        guard_text.append(value)
        if content_type == "text/html":
            parser = _HTMLText()
            parser.feed(value)
            parser.close()
            # 完整HTML文本也经guard，不能先去掉含marker的标签/脚本再放行。
            text.append("".join(parser.values))
        else:
            text.append(value)
    # message/rfc822和附件不解码、不递归，不算客户原话。


def _one(headers: Message, name: str) -> str | None:
    values = [
        value for key, value in headers.raw_items() if key.casefold() == name.casefold()
    ]
    return values[0] if len(values) == 1 else None


def parse_inbound_content(raw_mime: bytes) -> InboundContent:
    """Raw授权读取后的纯解析口；调用层须对guard_body和body分别检查，不能伪造Provider元数据。"""
    if not isinstance(raw_mime, bytes) or not raw_mime:
        return InboundContent(disposition=D.MALFORMED)
    if len(raw_mime) > MIME_BYTES:
        return InboundContent(disposition=D.TOO_LARGE)
    try:
        budget = _Budget()
        headers, payload = budget.header(raw_mime, 1)
        if headers.get_content_type() == "multipart/report":
            return InboundContent(disposition=D.SKIPPED_DELIVERY_REPORT)
        text: list[str] = []
        guard_text: list[str] = []
        _walk(headers, payload, budget, 1, text, guard_text)
        body = "\n".join(text)
        return InboundContent(
            disposition=D.CANDIDATE if body.strip() else D.NO_BODY,
            subject=str(headers.get("Subject", "")),
            body=body,
            guard_body="\n".join(guard_text),
        )
    except _ParseLimit as error:
        return InboundContent(disposition=error.disposition)
    except Exception:  # noqa: BLE001 纯解析固定失败，不带输入
        return InboundContent(disposition=D.MALFORMED)


def parse_inbound_message(
    message_ref: str, result: GmailInboundRawResult
) -> ProviderInboundItem:
    """固定disposition，无效客户输入不会成为网络临时错误。"""
    digest = hashlib.sha256(message_ref.encode("utf-8")).hexdigest()
    if result.status != "raw":
        return ProviderInboundItem(
            provider_ref_digest=digest, disposition=D(result.status)
        )
    raw = result.raw_mime
    if not raw or result.internal_date is None:
        return ProviderInboundItem(
            provider_ref_digest=digest, disposition=D.MALFORMED, raw_mime=raw
        )
    item = ProviderInboundItem(
        provider_ref_digest=digest,
        disposition=D.CANDIDATE,
        raw_mime=raw,
        internal_date=result.internal_date,
    )
    if set(result.labels) & {"SENT", "DRAFT", "SPAM", "TRASH"}:
        return item.model_copy(update={"disposition": D.SKIPPED_LABEL})
    parsed_content = parse_inbound_content(raw)
    if parsed_content.disposition not in {D.CANDIDATE, D.NO_BODY}:
        return item.model_copy(update={"disposition": parsed_content.disposition})
    try:
        headers, _payload = _Budget().header(raw, 1)
        content = parsed_content.body
        subject = parsed_content.subject
        external, reply, date = (
            _one(headers, "Message-ID"),
            _one(headers, "In-Reply-To"),
            _one(headers, "Date"),
        )
        disposition = D.CANDIDATE
        if external is None:
            disposition = (
                D.MISSING_MESSAGE_ID
                if headers.get("Message-ID") is None
                else D.INVALID_MESSAGE_ID
            )
        elif _ID.fullmatch(external) is None:
            disposition = D.INVALID_MESSAGE_ID
        elif reply is None or _REPLY.fullmatch(reply) is None:
            disposition = D.INVALID_IN_REPLY_TO
        sent_at = None
        try:
            if (
                date is None
                or "\r" in date
                or "\n" in date
                or _ZONE.search(date) is None
            ):
                raise ValueError()
            sent_at = parsedate_to_datetime(date)
            if sent_at.tzinfo is None:
                raise ValueError()
            sent_at = sent_at.astimezone(UTC)
        except (ValueError, TypeError, OverflowError):
            sent_at = None
            if disposition is D.CANDIDATE:
                disposition = D.INVALID_SENT_AT
        if not content.strip() and disposition is D.CANDIDATE:
            disposition = D.NO_BODY
        return item.model_copy(
            update={
                "disposition": disposition,
                "external_message_id": external
                if external and _ID.fullmatch(external)
                else None,
                "in_reply_to": reply if reply and _REPLY.fullmatch(reply) else None,
                "sent_at": sent_at,
                "subject": subject,
                "body": content,
                "guard_body": parsed_content.guard_body,
            }
        )
    except _ParseLimit as error:
        return item.model_copy(update={"disposition": error.disposition})
    except Exception:  # noqa: BLE001 MIME/charset/HTML异常不包含客户原文
        return item.model_copy(update={"disposition": D.MALFORMED})
