"""QQ 项目回信：指定成功Attempt的服务端查询，精确授权后才读取正文。"""
from __future__ import annotations

import base64
import json
import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.parser import BytesHeaderParser
from typing import Protocol

from pydantic import Field

from connectors.gmail.inbound_mime import parse_inbound_message
from connectors.gmail.inbound_transport import GmailInboundRawResult
from shared.schemas.email_inbound import (
    CURSOR_BYTES,
    HEADER_BYTES,
    MIME_BYTES,
    PAGE_BYTES,
    PAGE_ITEMS,
    InboundDTO,
    InboundError,
    InboundRoute,
    ProviderInboundPage,
)
from shared.schemas.email_inbound_diagnostics import (
    InboundStage,
    diagnosed,
    inbound_stage,
    safe_failure,
)
from shared.schemas.identifiers import EmployeeId, MessageId

MAX_SEARCH_RESULTS = 100
_ATTEMPT = re.compile(r"mat_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
_PROJECT = re.compile(r"<[a-z0-9-]{1,32}\.[0-9a-f]{64}@messages\.tradeos\.invalid>")


class QQCursor(InboundDTO):
    route: InboundRoute
    attempt_id: str = Field(strict=True, pattern=r"^mat_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    outbound_message_header: str | None = None
    uidvalidity: int | None = Field(default=None, strict=True, gt=0, le=4294967295)
    last_uid: int = Field(default=0, strict=True, ge=0, le=4294967295)
    window_end: int | None = Field(default=None, strict=True, ge=0, le=4294967295)


def encode_cursor(state: QQCursor) -> str:
    raw = state.model_dump_json().encode()
    value = "qic2." + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(value) > CURSOR_BYTES:
        raise InboundError()
    return value


def decode_cursor(value: str, route: InboundRoute) -> QQCursor:
    try:
        if not value.startswith("qic2.") or len(value) > CURSOR_BYTES:
            raise ValueError()
        data = value[5:]
        state = QQCursor.model_validate(json.loads(base64.b64decode(
            data + "=" * (-len(data) % 4), altchars=b"-_", validate=True,
        )))
        if ((state.outbound_message_header is not None and not _PROJECT.fullmatch(state.outbound_message_header))
            or (state.uidvalidity is not None and state.outbound_message_header is None)):
            raise ValueError()
        if state.route != route or encode_cursor(state) != value:
            raise ValueError()
        if state.uidvalidity is None and (state.last_uid or state.window_end is not None):
            raise ValueError()
        if state.window_end is not None and state.window_end < state.last_uid:
            raise ValueError()
        return state
    except Exception:  # noqa: BLE001 外部错误固定脱敏，不回显头或凭证
        raise InboundError() from None


def initial_cursor(route: InboundRoute, *, attempt_id: str | None) -> str:
    """首次Gateway参数已绑定指定Attempt；无目标或旧扫描游标不能进入查询。"""
    if not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
        raise InboundError()
    return encode_cursor(QQCursor(route=route, attempt_id=attempt_id))


@dataclass(frozen=True, repr=False)
class ReplyHeaders:
    message_id: str
    in_reply_to: str
    sender: str
    recipient: str
    date: str


def reply_headers(raw: bytes) -> ReplyHeaders | None:
    """只解析必需头；缺失、多值、抄送、畸形头一律不读取正文。"""
    try:
        marker = raw.find(b"\r\n\r\n")
        if marker < 0:
            marker = raw.find(b"\n\n")
        if marker < 0 or marker > HEADER_BYTES:
            return None
        headers = BytesHeaderParser(policy=policy.default).parsebytes(raw[:marker + 4])
        if headers.defects or headers.get_all("Cc"):
            return None
        names = ("Message-ID", "In-Reply-To", "From", "To", "Date")
        if any(len(headers.get_all(name, [])) != 1 for name in names):
            return None
        if any(getattr(headers[name], "defects", ()) for name in names):
            return None
        external, reply = str(headers["Message-ID"]), str(headers["In-Reply-To"])
        if not _ID.fullmatch(external) or not _PROJECT.fullmatch(reply):
            return None
        addresses = []
        for name in ("From", "To"):
            header = headers[name]
            if len(header.addresses) != 1 or any(g.display_name for g in header.groups):
                return None
            address = header.addresses[0].addr_spec
            if not re.fullmatch(r"[0-9]{5,12}@qq\.com", address):
                return None
            addresses.append(address)
        return ReplyHeaders(external, reply, addresses[0], addresses[1], str(headers["Date"]))
    except Exception:  # noqa: BLE001 外部错误固定脱敏，不回显头或凭证
        return None


@dataclass(frozen=True, repr=False)
class ProjectReplyGrant:
    """只由当前持久发送事实提供；邮件头本身不能授予授权。"""
    route: InboundRoute
    employee_id: EmployeeId
    outbound_message_id: MessageId
    outbound_message_header: str
    sender: str
    recipient: str
    attempt_id: str


class ProjectReplyScope(Protocol):
    async def target(
        self, route: InboundRoute, employee_id: EmployeeId,
    ) -> ProjectReplyGrant | None: ...

    async def resolve(
        self, route: InboundRoute, employee_id: EmployeeId, headers: ReplyHeaders,
    ) -> ProjectReplyGrant | None: ...


@dataclass(frozen=True, repr=False)
class HeaderResult:
    uid: int
    size: int
    raw: bytes


class QQInboundSession(Protocol):
    async def snapshot(self) -> tuple[int, int]: ...
    async def search_replies(self, target: ProjectReplyGrant, after_uid: int, before_uid: int) -> tuple[int, ...]: ...
    async def headers(self, uid: int) -> HeaderResult | None: ...
    async def body(self, uid: int, size: int) -> bytes: ...


class QQInboundTransport(Protocol):
    def session(self) -> AbstractAsyncContextManager[QQInboundSession]: ...


class QQInboundReader:
    """使用同一个只读 IMAP 会话；仅查询指定成功Attempt，不归档或调用模型。"""
    def __init__(
        self, route: InboundRoute, employee_id: EmployeeId,
        transport: QQInboundTransport, scope: ProjectReplyScope,
        *, address: str,
    ) -> None:
        if not re.fullmatch(r"[0-9]{5,12}@qq\.com", address):
            raise InboundError()
        self.route = route
        self._employee = employee_id
        self._transport = transport
        self._scope = scope
        self._address = address

    def __repr__(self) -> str:
        return "QQInboundReader()"

    @diagnosed(InboundStage.TARGET)
    async def _target(self) -> ProjectReplyGrant:
        grant = await self._scope.target(self.route, self._employee)
        if (grant is None or grant.route != self.route or grant.employee_id != self._employee
            or grant.sender != self._address or grant.recipient != "986647123@qq.com"
            or not grant.outbound_message_id or not _ATTEMPT.fullmatch(grant.attempt_id)
            or not _PROJECT.fullmatch(grant.outbound_message_header)
            or not grant.outbound_message_header.startswith("<" + self.route.route_id + ".")):
            raise InboundError()
        return grant

    @diagnosed(InboundStage.AUTHORIZE)
    async def _grant(self, header: ReplyHeaders) -> ProjectReplyGrant | None:
        if header.recipient != self._address or not header.in_reply_to.startswith(
            "<" + self.route.route_id + "."
        ):
            return None
        grant = await self._scope.resolve(self.route, self._employee, header)
        if grant is None:
            return None
        if (
            grant.route != self.route or grant.employee_id != self._employee
            or not grant.outbound_message_id
            or grant.outbound_message_header != header.in_reply_to
            or grant.sender != header.recipient or grant.recipient != header.sender
        ):
            raise InboundError()
        return grant

    @diagnosed(InboundStage.PAGE)
    async def fetch_inbound_page(
        self, mailbox_alias: str, cursor: str, page_limit: int,
    ) -> ProviderInboundPage:
        if mailbox_alias != self.route.mailbox_alias or type(page_limit) is not int or not 1 <= page_limit <= PAGE_ITEMS:
            raise InboundError()
        state = decode_cursor(cursor, self.route)
        try:
            target = await self._target()
            if (state.attempt_id != target.attempt_id
                or (state.outbound_message_header is not None
                    and state.outbound_message_header != target.outbound_message_header)):
                raise InboundError()
            async with self._transport.session() as session:
                validity, next_uid = await session.snapshot()
                if type(validity) is not int or type(next_uid) is not int or not 1 <= validity <= 4294967295 or not 1 <= next_uid <= 4294967296:
                    raise InboundError()
                if state.uidvalidity is not None and state.uidvalidity != validity:
                    raise InboundError()
                if next_uid - 1 < state.last_uid:
                    raise InboundError()
                horizon = state.window_end if state.window_end is not None else next_uid - 1
                if horizon >= next_uid:
                    raise InboundError()
                last = state.last_uid
                items = []
                used = 0
                candidates = await session.search_replies(target, last, horizon) if last < horizon else ()
                if (not isinstance(candidates, tuple) or len(candidates) > MAX_SEARCH_RESULTS
                    or any(type(uid) is not int or not last < uid <= horizon for uid in candidates)
                    or tuple(sorted(set(candidates))) != candidates):
                    raise InboundError()
                if (await session.snapshot())[0] != validity:
                    raise InboundError()
                processed = 0
                for uid in candidates[:page_limit]:
                    if await self._target() != target:
                        raise InboundError()
                    result = await session.headers(uid)
                    if result is None:
                        processed += 1
                        last = uid
                        continue
                    if result.uid != uid or len(result.raw) > HEADER_BYTES or type(result.size) is not int or result.size <= 0:
                        raise InboundError()
                    header = reply_headers(result.raw)
                    if (header is None or header.in_reply_to != target.outbound_message_header
                        or header.sender != target.recipient or header.recipient != target.sender):
                        processed += 1
                        last = uid
                        continue
                    grant = await self._grant(header)
                    if grant != target or (await session.snapshot())[0] != validity:
                        raise InboundError()
                    if result.size > MIME_BYTES:
                        raise InboundError()
                    if used + result.size > PAGE_BYTES:
                        break
                    raw = await session.body(uid, result.size)
                    # UID 与选定头必须一致；当前权限/发送关联撤销后不得交付归档。
                    if len(raw) != result.size or reply_headers(raw) != header:
                        raise InboundError()
                    if await self._grant(header) != grant:
                        raise InboundError()
                    current_validity, _ = await session.snapshot()
                    if current_validity != validity:
                        raise InboundError()
                    ref = f"qq:{validity}:{uid}"
                    with inbound_stage(InboundStage.PARSE):
                        items.append(parse_inbound_message(ref, GmailInboundRawResult(
                            status="raw", raw_mime=raw, internal_date=datetime.now(UTC),
                        )))
                    used += len(raw)
                    processed += 1
                    last = uid
                if processed == len(candidates):
                    last = horizon
                if await self._target() != target or (await session.snapshot())[0] != validity:
                    raise InboundError()
                next_state = QQCursor(
                    route=self.route, attempt_id=target.attempt_id,
                    outbound_message_header=target.outbound_message_header,
                    uidvalidity=validity, last_uid=last,
                    window_end=horizon if last < horizon else None,
                )
                return ProviderInboundPage(
                    route=self.route, starting_cursor=cursor,
                    next_cursor=encode_cursor(next_state), items=tuple(items),
                )
        except Exception as error:  # noqa: BLE001 只复制白名单字段，不保留异常链
            failure = safe_failure(InboundStage.PAGE, error, 0)
        raise failure from None
