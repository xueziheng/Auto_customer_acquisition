"""人工确认的有限历史读取；只供 Gateway 运维恢复，不注册为自动入站回退。"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime

from connectors.gmail.inbound_mime import parse_inbound_content
from connectors.qq_mail.inbound import ProjectReplyGrant
from connectors.qq_mail.inbound_transport import (
    QQImapInboundTransport,
    _Session,
    _thread_call,
)
from shared.schemas.email_inbound import HEADER_BYTES, MIME_BYTES, InboundError
from shared.schemas.email_inbound_diagnostics import require_protocol_ok


@dataclass(frozen=True)
class ConfirmedHistory:
    """由受信操作者提供的原信及已确认回复内容，不能从网页/邮件指令推导。"""
    subject: str = field(repr=False)
    original_body: str = field(repr=False)
    sent_at: datetime
    reply_phrases: tuple[str, ...] = field(repr=False)
    reply_window: timedelta = timedelta(minutes=30)


@dataclass(frozen=True)
class ConfirmedReply:
    external_id: str = field(repr=False)
    original_reply_to: str = field(repr=False)
    sent_at: datetime
    raw: bytes = field(repr=False)


def _normalized(value: str) -> str:
    return re.sub(r"[\s>]", "", unicodedata.normalize("NFKC", value))


def checked_headers(raw: bytes, grant: ProjectReplyGrant, confirmed: ConfirmedHistory) -> tuple[str, str, datetime] | None:
    """主题、双方、原始编号、原始日期同时检查；这仍不是自动关联证明。"""
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    if message.defects or any(len(message.get_all(key, [])) != 1 for key in
        ("From", "To", "Subject", "Date", "Message-ID", "In-Reply-To")):
        return None
    if message.get_all("Cc") or message.get_all("Bcc"):
        return None
    sender = getaddresses(message.get_all("From", []))
    if len(sender) != 1 or sender[0][1] != grant.recipient:
        return None
    to = getaddresses(message.get_all("To", []))
    if len(to) != 1 or to[0][1] != grant.sender:
        return None
    subject = re.sub(r"^(?:Re:\s*)+", "", str(message["Subject"]), flags=re.IGNORECASE)
    if subject != confirmed.subject:
        return None
    external, reply_to = str(message["Message-ID"]), str(message["In-Reply-To"])
    if not all(re.fullmatch(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@qq\.com>", value)
               for value in (external, reply_to)) or external == reply_to:
        return None
    try:
        stamp = parsedate_to_datetime(str(message["Date"]))
        if stamp.tzinfo is None:
            return None
        stamp = stamp.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None
    if not confirmed.sent_at <= stamp <= confirmed.sent_at + confirmed.reply_window:
        return None
    return external, reply_to, stamp


def checked_bodies(rows: list[tuple[bytes, tuple[str, str, datetime]]], grant: ProjectReplyGrant,
                   confirmed: ConfirmedHistory) -> tuple[ConfirmedReply, ...]:
    """完整原件复查；必须逐一对应人工确认的回复原话并包含原信引用。"""
    phrases = tuple(_normalized(value) for value in confirmed.reply_phrases)
    found: list[str] = []
    result: list[ConfirmedReply] = []
    for raw, header in rows:
        if checked_headers(raw, grant, confirmed) != header:
            raise InboundError()
        content = parse_inbound_content(raw)
        body = _normalized(content.body)
        matches = [phrase for phrase in phrases if body.startswith(phrase)]
        if (content.disposition != "candidate" or len(matches) != 1
            or _normalized(confirmed.original_body) not in body):
            raise InboundError()
        found.append(matches[0])
        result.append(ConfirmedReply(*header, raw))
    if (sorted(found) != sorted(phrases) or len({r.external_id for r in result}) != len(phrases)
        or len({r.original_reply_to for r in result}) != 1):
        raise InboundError()
    return tuple(sorted(result, key=lambda value: value.sent_at))


class _HistorySession(_Session):
    async def search_day(self, sent_at: datetime) -> tuple[int, ...]:
        def search() -> tuple[int, ...]:
            # QQ 拒绝原 HEADER 查询；这里仅用单日查询，最多20个头，正文另授权。
            self.connection.budget = HEADER_BYTES
            self.connection.literal_limit = HEADER_BYTES
            def stamp(value: datetime) -> str:
                months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
                return f"{value.day:02d}-{months[value.month - 1]}-{value.year}"
            status, rows = self.connection.uid("SEARCH", None, "SINCE", stamp(sent_at),  # type: ignore[arg-type]  # imaplib允许None省略charset
                                               "BEFORE", stamp(sent_at + timedelta(days=1)))
            require_protocol_ok(status)
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], bytes):
                raise InboundError()
            tokens = rows[0].split()
            if len(tokens) > 20 or any(not re.fullmatch(rb"[1-9][0-9]{0,9}", token) for token in tokens):
                raise InboundError()
            uids = tuple(int(token) for token in tokens)
            if uids != tuple(sorted(set(uids))):
                raise InboundError()
            return uids
        return await _thread_call(search)

    async def recovery_header(self, uid: int) -> tuple[int, bytes] | None:
        data = await _thread_call(self._fetch, uid,
            "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO FROM TO CC BCC DATE SUBJECT)])", HEADER_BYTES)
        if data is None:
            return None
        meta, raw = data
        sizes = re.findall(rb"\bRFC822.SIZE ([0-9]+)\b", meta)
        if len(sizes) != 1:
            raise InboundError()
        return int(sizes[0]), raw


async def read_confirmed_history(transport: QQImapInboundTransport, grant: ProjectReplyGrant,
    confirmed: ConfirmedHistory, authorize: Callable[[], Awaitable[ProjectReplyGrant]],
) -> tuple[ConfirmedReply, ...]:
    """一次只读连接；完整核对所有候选后才返回，不改QQ已读状态或持久入站游标。"""
    if (not 1 <= len(confirmed.reply_phrases) <= 2
        or len(set(confirmed.reply_phrases)) != len(confirmed.reply_phrases)
        or not timedelta(0) < confirmed.reply_window <= timedelta(hours=1)
        or any(not value.strip() for value in confirmed.reply_phrases)
        or len(_normalized(confirmed.original_body)) < 100
        or confirmed.sent_at.tzinfo is None or await authorize() != grant):
        raise InboundError()
    async with transport.session() as base:
        session = _HistorySession(base.connection)
        uidvalidity, uidnext = await session.snapshot()
        candidates = []
        for uid in await session.search_day(confirmed.sent_at):
            if not 0 < uid < uidnext or await authorize() != grant:
                raise InboundError()
            item = await session.recovery_header(uid)
            if item is None:
                continue
            size, raw = item
            header = checked_headers(raw, grant, confirmed)
            if header is not None:
                if not 0 < size <= MIME_BYTES:
                    raise InboundError()
                candidates.append((uid, size, header))
        if len(candidates) != len(confirmed.reply_phrases):
            raise InboundError()
        rows = []
        for uid, size, header in candidates:
            if (await session.snapshot())[0] != uidvalidity or await authorize() != grant:
                raise InboundError()
            raw = await session.body(uid, size)
            rows.append((raw, header))
        if (await session.snapshot())[0] != uidvalidity or await authorize() != grant:
            raise InboundError()
        return checked_bodies(rows, grant, confirmed)
