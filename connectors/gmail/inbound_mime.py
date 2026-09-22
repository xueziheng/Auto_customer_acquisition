"""有界MIME技术解析；不关联客户、不分类需求、不展开附件为原话。"""

from __future__ import annotations

import base64
import codecs
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
_DATE = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),[ \t]+)?"
    r"(?:0?[1-9]|[12][0-9]|3[01])[ \t]+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[ \t]+"
    r"(?:19[0-9]{2}|[2-9][0-9]{3})[ \t]+(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?[ \t]+"
    r"(?:[+-](?:[01][0-9]|2[0-3])[0-5][0-9]|UT|GMT|[ECMP][SD]T)",
    re.IGNORECASE | re.ASCII,
)
# 只把受信的标准库文本codec名传给decoder；不查询邮件任意指定的codec注册项。
_TEXT_CHARSETS = {
    "ascii": "ascii",
    "us-ascii": "ascii",
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "cp65001": "utf-8",
    "utf-16": "utf-16",
    "utf16": "utf-16",
    "utf-16le": "utf-16-le",
    "utf-16-le": "utf-16-le",
    "utf-16be": "utf-16-be",
    "utf-16-be": "utf-16-be",
    "utf-32": "utf-32",
    "utf32": "utf-32",
    "utf-32le": "utf-32-le",
    "utf-32-le": "utf-32-le",
    "utf-32be": "utf-32-be",
    "utf-32-be": "utf-32-be",
    "latin1": "iso-8859-1",
    "latin-1": "iso-8859-1",
    "gb2312": "gb2312",
    "gb-2312": "gb2312",
    "gb-2312-80": "gb2312",
    "euc-cn": "gb2312",
    "gbk": "gbk",
    "cp936": "gbk",
    "gb18030": "gb18030",
    "big5": "big5",
    "big5-hkscs": "big5hkscs",
    "big5hkscs": "big5hkscs",
    "shift-jis": "shift_jis",
    "shiftjis": "shift_jis",
    "sjis": "shift_jis",
    "cp932": "cp932",
    "euc-jp": "euc_jp",
    "eucjp": "euc_jp",
    "iso-2022-jp": "iso2022_jp",
    "iso2022-jp": "iso2022_jp",
    "euc-kr": "euc_kr",
    "euckr": "euc_kr",
    "cp949": "cp949",
    "iso-2022-kr": "iso2022_kr",
    "iso2022-kr": "iso2022_kr",
    "koi8-r": "koi8-r",
    "koi8-u": "koi8-u",
    **{
        f"iso{separator}8859-{number}": f"iso-8859-{number}"
        for separator in ("", "-")
        for number in (*range(1, 12), *range(13, 17))
    },
    **{
        f"{prefix}{number}": f"cp{number}"
        for prefix in ("cp", "windows-")
        for number in range(1250, 1259)
    },
}
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
        requested_charset = (
            (headers.get_content_charset() or "ascii").replace("_", "-").lower()
        )
        charset = _TEXT_CHARSETS.get(requested_charset)
        if charset is None:
            raise _ParseLimit(D.MALFORMED)
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
        # 支持的文本codec每个有界chunk只有常数倍扩张；仍逐块核UTF-8总预算。
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


def _current_plain_segments(value: str) -> tuple[str, ...]:
    """只排除明确引用行/历史分隔，保留连续片段，不宣称识别所有邮件客户端。"""
    segments: list[str] = []
    current: list[str] = []
    for line in value.splitlines(keepends=True):
        if re.match(
            r"(?i)^\s*(?:On .+ wrote:|-{2,}\s*(?:Original|Forwarded) Message|Begin forwarded message:)",
            line,
        ):
            break
        if line.lstrip().startswith(">"):
            if current:
                segments.append("".join(current))
                if len(segments) > 200:
                    return tuple(segments)
                current = []
        else:
            current.append(line)
    if current:
        segments.append("".join(current))
    return tuple(segment for segment in segments if segment.strip())


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []
        self.hidden = 0
        self.quote_stack: list[str] = []
        self.history_suffix = False
        self.current: list[str] = []
        self.segments: list[str] = []

    def _boundary(self) -> None:
        if self.current:
            if len(self.segments) <= 200:
                self.segments.append("".join(self.current))
            self.current = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id") == "divRplyFwdMsg":
            # Outlook历史头是后缀分隔符；关闭头容器不恢复当前表达。
            self._boundary()
            self.history_suffix = True
        quoted = (
            tag == "blockquote"
            or bool(
                {"gmail_quote", "yahoo_quoted"}
                & set((attributes.get("class") or "").split())
            )
        )
        if quoted or self.quote_stack:
            self._boundary()
            if tag not in {
                "area", "base", "br", "col", "embed", "hr", "img",
                "input", "link", "meta", "source", "track", "wbr",
            }:
                self.quote_stack.append(tag)
        if tag in {"script", "style"}:
            self._boundary()
            self.hidden += 1
        if tag in {"p", "br", "div"} and not self.hidden:
            self.values.append("\n")
            if not self.quote_stack and not self.history_suffix:
                self.current.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if self.quote_stack and tag == self.quote_stack[-1]:
            self.quote_stack.pop()

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.values.append(data)
            if not self.quote_stack and not self.history_suffix:
                self.current.append(data)

    def current_segments(self) -> tuple[str, ...]:
        self._boundary()
        if self.quote_stack:
            return ()
        return tuple(
            part
            for segment in self.segments
            for part in _current_plain_segments(segment)
        )


def _walk(
    headers: Message,
    body: bytes,
    budget: _Budget,
    depth: int,
    text: list[str],
    guard_text: list[str],
    evidence_segments: list[str],
    evidence_available: list[bool],
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
                _walk(
                    child,
                    payload,
                    budget,
                    depth + 1,
                    text,
                    guard_text,
                    evidence_segments,
                    evidence_available,
                    allow_text,
                )
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
            if len(evidence_segments) <= 200:
                evidence_segments.extend(parser.current_segments())
            if parser.quote_stack:
                evidence_available[0] = False
        else:
            text.append(value)
            if len(evidence_segments) <= 200:
                evidence_segments.extend(_current_plain_segments(value))
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
        evidence_segments: list[str] = []
        evidence_available = [True]
        _walk(
            headers,
            payload,
            budget,
            1,
            text,
            guard_text,
            evidence_segments,
            evidence_available,
        )
        body = "\n".join(text)
        return InboundContent(
            disposition=D.CANDIDATE if body.strip() else D.NO_BODY,
            subject=str(headers.get("Subject", "")),
            body=body,
            guard_body="\n".join(guard_text),
            evidence_segments=tuple(evidence_segments)
            if len(evidence_segments) <= 200
            else (),
            evidence_available=evidence_available[0] and len(evidence_segments) <= 200,
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
                or _DATE.fullmatch(date) is None
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
