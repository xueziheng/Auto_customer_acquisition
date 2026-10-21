"""独立普通收件连接器：固定QQ只读IMAP，有界UID页，不消费私人回信许可。"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email import policy
from email.message import MIMEPart
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from connectors.qq_mail.inbound import QQInboundTransport
from connectors.qq_mail.inbound_transport import _Session, _thread_call
from connectors.qq_mail.mime_structure import _Wire
from shared.business_mail import BusinessFetchPage, RawBusinessMail
from shared.schemas.email_inbound import HEADER_BYTES, MIME_BYTES, InboundError
from shared.schemas.email_inbound_diagnostics import require_protocol_ok
from shared.schemas.qq_trial import QQTrialStopped


def _parse_business_body(result: object, uid: int) -> tuple[bytes, int | None]:
    """literal与协议分开校验；单响应必须完整闭合，不能混入其他UID或尾部。"""
    if (
        not isinstance(result, list)
        or len(result) != 2
        or not isinstance(result[0], tuple)
        or len(result[0]) != 2
        or not all(isinstance(value, bytes) for value in result[0])
        or not isinstance(result[1], bytes)
    ):
        raise InboundError()
    meta, raw = result[0]
    marker = re.search(rb"BODY\[\]<0> \{([0-9]{1,7})\}$", meta)
    if (
        marker is None
        or re.match(rb"[1-9][0-9]{0,9} \(", meta) is None
        or int(marker[1]) != len(raw)
        or not 0 < len(raw) <= MIME_BYTES
        or len(meta) + len(result[1]) > HEADER_BYTES
    ):
        raise InboundError()
    # 复用有界协议token解析器，但原件字节绝不作为协议再解释。
    wire = _Wire(meta[: marker.start()] + b"BODY[] NIL" + result[1])
    try:
        sequence, attributes = wire.value(), wire.value()
        wire.space()
        if (
            type(sequence) is not int
            or sequence <= 0
            or not isinstance(attributes, list)
            or len(attributes) % 2
            or wire.pos != len(wire.raw)
        ):
            raise InboundError()
        values = {}
        for key, value in zip(attributes[::2], attributes[1::2], strict=True):
            if not isinstance(key, str) or key in values:
                raise InboundError()
            values[key] = value
        reported = values.get("RFC822.SIZE")
        if (
            not {"UID", "BODY[]"} <= set(values)
            or set(values) - {"UID", "BODY[]", "RFC822.SIZE", "INTERNALDATE"}
            or type(values["UID"]) is not int
            or values["UID"] != uid
            or values["BODY[]"] is not None
            or (
                "RFC822.SIZE" in values and (type(reported) is not int or reported <= 0)
            )
            or (
                "INTERNALDATE" in values
                and not isinstance(values["INTERNALDATE"], bytes)
            )
        ):
            raise InboundError()
        return raw, reported if type(reported) is int else None
    except QQTrialStopped:
        raise InboundError() from None


def _read_business_body(session: _Session, uid: int) -> tuple[bytes, int | None]:
    """普通收信使用固定硬界；声明大小仅作一致性证据，不决定可读literal大小。"""
    connection = session.connection
    connection.literal_limit = MIME_BYTES
    connection.budget = MIME_BYTES + HEADER_BYTES
    status, result = connection.uid(
        "FETCH", str(uid), f"(UID BODY.PEEK[]<0.{MIME_BYTES}>)"
    )
    require_protocol_ok(status)
    return _parse_business_body(result, uid)


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def parse_business_mime(
    raw: bytes, *, uid: int, uidvalidity: int, received_at: datetime | None = None
) -> RawBusinessMail:
    """有界提取纯文本；附件不交模型。异常原件仍归档并形成待确认记录。"""
    if not 0 < len(raw) <= MIME_BYTES:
        raise InboundError()
    failure = None
    sender, subject, body, message_header = "", "", "", None
    occurred = received_at or datetime.now(UTC)
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        sender = ", ".join(
            addr for _, addr in getaddresses(message.get_all("From", []))
        )[:1000]
        subject = str(message.get("Subject", ""))[:2000]
        ids = message.get_all("Message-ID", [])
        if len(ids) == 1 and re.fullmatch(r"<[^<>\s]{1,998}>", str(ids[0])):
            message_header = str(ids[0])
        dates = message.get_all("Date", [])
        if len(dates) == 1:
            try:
                parsed = parsedate_to_datetime(str(dates[0]))
                if parsed.tzinfo is not None:
                    pass  # Date是发件人声明；接收事实由IMAP INTERNALDATE传入。
            except (TypeError, ValueError, OverflowError):
                failure = "invalid_date"
        plain: list[str] = []
        html: list[str] = []
        parts: list[MIMEPart] = [message]
        while parts:
            part = parts.pop()
            # walk()会继续进入被跳过的附件容器；必须在遍历前剪枝整棵附件子树。
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_maintype() == "multipart":
                parts.extend(reversed(list(part.iter_parts())))
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            value = part.get_content()
            if not isinstance(value, str):
                continue
            if part.get_content_type() == "text/plain":
                plain.append(value)
            else:
                parser = _PlainHTML()
                parser.feed(value)
                html.append("".join(parser.parts))
        body = "\n".join(plain or html)
        if len(body) > 50000:
            body, failure = body[:50000], "body_limit"
        if not sender or not body.strip() or message.defects:
            failure = failure or "mime_review"
    except Exception:  # noqa: BLE001 -- 不回显原文和解析器异常
        failure = "mime_review"
    return RawBusinessMail(
        uid=uid,
        uidvalidity=uidvalidity,
        sender=sender,
        subject=subject,
        body_text=body,
        message_header=message_header,
        received_at=occurred,
        parse_failure=failure,
        raw_mime=raw,
    )


def content_hash(message: RawBusinessMail) -> str:
    """排除传输添加的Received等头，以确定性内容摘要去除UID重置后的同件副本。"""
    return hashlib.sha256(
        (
            "\0".join(
                (
                    message.sender,
                    message.subject,
                    message.body_text,
                    message.message_header
                    or hashlib.sha256(message.raw_mime).hexdigest(),
                )
            )
        ).encode()
    ).hexdigest()


class QQBusinessInboundConnector:
    def __init__(
        self, transport: QQInboundTransport, *, initial_uid_window: int = 500, page_size: int = 20
    ) -> None:
        if not 1 <= initial_uid_window <= 2000 or not 1 <= page_size <= 50:
            raise ValueError("普通来信读取范围无效")
        self.transport, self.window, self.page_size = (
            transport,
            initial_uid_window,
            page_size,
        )

    async def fetch(
        self, *, uidvalidity: int | None, after_uid: int, before_uid: int | None = None
    ) -> BusinessFetchPage:
        """每页只读有限UID，缺失UID经SEARCH确认；任何失败都不返回推进后的游标。"""
        async with self.transport.session() as session:
            if not isinstance(session, _Session):
                raise InboundError()
            validity, next_uid = await session.snapshot()
            after = (
                max(0, next_uid - self.window - 1)
                if validity != uidvalidity
                else after_uid
            )
            end = (
                min(next_uid - 1, before_uid)
                if before_uid is not None
                else next_uid - 1
            )
            upper = min(end, after + self.page_size)
            if upper <= after:
                return BusinessFetchPage(
                    uidvalidity=validity,
                    after_uid=after,
                    next_uid=after,
                    snapshot_end_uid=end,
                    messages=(),
                )
            connection = session.connection

            def search() -> tuple[int, ...]:
                connection.budget = HEADER_BYTES
                connection.literal_limit = HEADER_BYTES
                status, data = connection.uid(
                    "SEARCH", None, "UID", f"{after + 1}:{upper}"  # type: ignore[arg-type]  # imaplib允许None省略charset
                )
                require_protocol_ok(status)
                if (
                    not isinstance(data, list)
                    or len(data) != 1
                    or not isinstance(data[0], bytes)
                ):
                    raise InboundError()
                values = data[0].split()
                if len(values) > self.page_size or any(
                    not re.fullmatch(rb"[1-9][0-9]{0,9}", v) for v in values
                ):
                    raise InboundError()
                uids = tuple(map(int, values))
                if tuple(sorted(set(uids))) != uids or any(
                    not after < uid <= upper for uid in uids
                ):
                    raise InboundError()
                return uids

            uids = await _thread_call(search)
            messages = []
            for uid in uids:
                header_result = await _thread_call(
                    session._fetch,
                    uid,
                    f"(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER]<0.{HEADER_BYTES}>)",
                    HEADER_BYTES,
                )
                if header_result is None:
                    raise InboundError()
                meta, raw_headers = header_result
                sizes = re.findall(rb"\bRFC822.SIZE ([0-9]+)\b", meta)
                dates = re.findall(rb'\bINTERNALDATE "([^"]+)"', meta)
                if len(sizes) != 1 or len(dates) != 1:
                    raise InboundError()
                try:
                    received = datetime.strptime(
                        dates[0].decode("ascii"), "%d-%b-%Y %H:%M:%S %z"
                    ).astimezone(UTC)
                except (ValueError, UnicodeError):
                    raise InboundError() from None
                size = int(sizes[0])
                if size <= 0:
                    raise InboundError()
                raw, reported = None, None
                if size <= MIME_BYTES:
                    raw, reported = await _thread_call(
                        _read_business_body, session, uid
                    )
                if raw is None or len(raw) == MIME_BYTES:
                    parsed = parse_business_mime(
                        raw_headers, uid=uid, uidvalidity=validity, received_at=received
                    )
                    messages.append(
                        parsed.model_copy(
                            update={
                                "parse_failure": "oversize",
                                "source_kind": "headers_only",
                            }
                        )
                    )
                else:
                    parsed = parse_business_mime(
                        raw, uid=uid, uidvalidity=validity, received_at=received
                    )
                    if len(raw) != size or (reported is not None and reported != size):
                        parsed = parsed.model_copy(
                            update={"parse_failure": "size_mismatch"}
                        )
                    messages.append(parsed)
            current_validity, _ = await session.snapshot()
            if current_validity != validity:
                raise InboundError()
            return BusinessFetchPage(
                uidvalidity=validity,
                after_uid=after,
                next_uid=upper,
                snapshot_end_uid=end,
                messages=tuple(messages),
            )
