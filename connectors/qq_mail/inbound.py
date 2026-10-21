"""QQ 项目回信：指定成功Attempt的服务端查询，精确授权后才读取正文。"""
from __future__ import annotations

import base64
import json
import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesHeaderParser
from typing import Protocol

from pydantic import Field

from connectors.gmail.inbound_mime import parse_inbound_message
from connectors.gmail.inbound_transport import GmailInboundRawResult
from connectors.qq_mail.mime_structure import (
    MimePlan,
    WireValue,
    match_headers,
    validate_structure,
    verify_mime,
)
from connectors.qq_mail.trial_address import AddressEvidence, trial_address
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
from shared.schemas.qq_trial import (
    QQTrialGate,
    QQTrialScope,
    QQTrialStopped,
    QQTrialWatermark,
)

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
    address_evidence: tuple[AddressEvidence, ...] = ()
    references: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class QQReplyHeaderScope:
    """受信持久别名的解析范围；不包含试验寿命或预算。"""
    provider_header: str
    sender: str
    recipient: str


def reply_headers(raw: bytes, *, expected_reply: str | None = None,
    trial_scope: QQTrialScope | QQReplyHeaderScope | None = None) -> ReplyHeaders | None:
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
        strict_names = names if trial_scope is None else ("Message-ID", "In-Reply-To", "Date")
        if any(getattr(headers[name], "defects", ()) for name in strict_names):
            return None
        external, reply = str(headers["Message-ID"]), str(headers["In-Reply-To"])
        references: tuple[str, ...] = ()
        if isinstance(trial_scope, QQReplyHeaderScope):
            values = headers.get_all("References", [])
            if len(values) > 1:
                return None
            if values:
                value = str(values[0]).strip()
                references = tuple(_ID.findall(value))
                if (not references or len(references) > MAX_SEARCH_RESULTS
                    or len(set(references)) != len(references)
                    or re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>(?:[ \t\r\n]+<[^<>\s@]+@[^<>\s@]+>)*", value) is None
                    or references[-1] != reply or external in references):
                    return None
        threaded = (
            isinstance(trial_scope, QQReplyHeaderScope)
            and expected_reply in references and references[-1] == reply
        )
        if not _ID.fullmatch(external) or (
            ((reply != expected_reply and not threaded) or not _ID.fullmatch(reply)) if expected_reply is not None
            else not _PROJECT.fullmatch(reply)
        ):
            return None
        if trial_scope is not None:
            if expected_reply != trial_scope.provider_header:
                return None
            evidence = tuple(trial_address(headers, raw, name, expected) for name, expected in (
                ("From", trial_scope.recipient), ("To", trial_scope.sender),
            ))
            if any(value is None for value in evidence):
                return None
            return ReplyHeaders(external, reply, trial_scope.recipient, trial_scope.sender,
                str(headers["Date"]), tuple(value for value in evidence if value is not None), references)
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


def _associated_unsafe_headers(raw: bytes, expected: str, sender: str, recipient: str, *, continuous: bool = False) -> bool:
    """精确引用且无法安全排除的候选必须核对；已明确不同的地址仍可跳过。

    不修正或信任带 defect 的地址，也不将服务器的包含搜索当作精确关联。
    """
    h = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    references = [re.sub(r"\r?\n(?=[ \t])", "", str(v))
                  for v in [*h.get_all("In-Reply-To", []), *(h.get_all("References", []) if continuous else [])]]
    if not any(re.fullmatch(r"(?:[ \t]*<[^<>\s@]+@[^<>\s@]+>)+[ \t]*", value)
               and expected in _ID.findall(value) for value in references) and not (
        continuous and any(expected in _ID.findall(str(v)) for v in h.get_all("References", []))
    ):
        return False
    for name, wanted in (("From", sender), ("To", recipient)):
        try:
            values = h.get_all(name, [])
            if len(values) == 1:
                value = values[0]
                if (not getattr(value, "defects", ()) and len(value.addresses) == 1
                        and not any(g.display_name for g in value.groups)):
                    address = value.addresses[0].addr_spec
                    if re.fullmatch(r"[0-9]{5,12}@qq\.com", address) and address != wanted:
                        return False
        except Exception:  # noqa: BLE001, S112 精确关联后解析失败不能排除；禁止日志记录原异常
            continue
    return True


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
    provider_header: str | None = None


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
    bodystructure: WireValue = None


class QQInboundSession(Protocol):
    async def snapshot(self) -> tuple[int, int]: ...
    async def search_replies(self, target: ProjectReplyGrant, after_uid: int, before_uid: int) -> tuple[int, ...]: ...
    async def headers(self, uid: int) -> HeaderResult | None: ...
    async def body(self, uid: int, size: int) -> bytes: ...


class QQInboundTransport(Protocol):
    def session(self) -> AbstractAsyncContextManager[QQInboundSession]: ...


def _trial_plain_mime(raw: bytes, structure: WireValue = None, total: int = 0) -> MimePlan | None:
    """先核验顶层与有界结构证据，缺失证据不能放行 multipart。"""
    h = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    names = ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding", "MIME-Version")
    if (h.defects or any(len(h.get_all(n, [])) > 1 for n in names)
            or any(getattr(h[n], "defects", ()) for n in names if n in h)):
        raise QQTrialStopped("mime_headers_invalid")
    if h.get_all("Bcc"):
        raise QQTrialStopped("bcc_not_allowed")
    if h.get_content_disposition() == "attachment" or h.get_filename() is not None:
        raise QQTrialStopped("attachments_not_allowed")
    if h.get_content_maintype() == "multipart" and structure is None:
        raise QQTrialStopped("mime_structure_unverified")
    if h.get_content_type() not in {"text/plain", "text/html", "multipart/alternative"}:
        raise QQTrialStopped("mime_type_not_supported")
    if structure is not None:
        plan = validate_structure(structure, total)
        match_headers(raw, plan)
        return plan
    return None


class QQInboundReader:
    """使用同一个只读 IMAP 会话；仅查询指定成功Attempt，不归档或调用模型。"""
    def __init__(
        self, route: InboundRoute, employee_id: EmployeeId,
        transport: QQInboundTransport, scope: ProjectReplyScope,
        *, address: str, trial_gate: QQTrialGate | None = None,
    ) -> None:
        if not re.fullmatch(r"[0-9]{5,12}@qq\.com", address):
            raise InboundError()
        self.route = route
        self._employee = employee_id
        self._transport = transport
        self._scope = scope
        self._address = address
        self._trial = trial_gate

    def __repr__(self) -> str:
        return "QQInboundReader()"

    async def _trial_mime(self, raw: bytes, structure: WireValue = None,
        total: int = 0, *, complete: bool = False) -> None:
        try:
            plan = _trial_plain_mime(raw, structure, total)
            if complete and plan is not None:
                verify_mime(raw, plan, total)
        except QQTrialStopped as error:
            # 原脱敏诊断不会携带供应商异常；先把固定停止原因写入本trial账本。
            if self._trial is not None:
                await self._trial.stop(error.reason)
            raise

    @diagnosed(InboundStage.TARGET)
    async def _target(self) -> ProjectReplyGrant:
        if self._trial is not None:
            await self._trial.check()
        grant = await self._scope.target(self.route, self._employee)
        if self._trial is not None and (grant is None or self._trial.scope.route != self.route
                or self._trial.scope.attempt_id != grant.attempt_id
                or self._trial.scope.employee_id != self._employee
                or self._trial.scope.sender != grant.sender
                or self._trial.scope.recipient != grant.recipient
                or self._trial.scope.canonical_header != grant.outbound_message_header):
            raise QQTrialStopped("scope_changed")
        if (grant is None or grant.route != self.route or grant.employee_id != self._employee
            or grant.sender != self._address or grant.recipient != "986647123@qq.com"
            or not grant.outbound_message_id or not _ATTEMPT.fullmatch(grant.attempt_id)
            or not _PROJECT.fullmatch(grant.outbound_message_header)
            or not grant.outbound_message_header.startswith("<" + self.route.route_id + ".")):
            raise InboundError()
        if grant.provider_header is not None and (
            self._trial is not None
            or not re.fullmatch(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>", grant.provider_header)
        ):
            raise InboundError()
        return grant

    @diagnosed(InboundStage.AUTHORIZE)
    async def _grant(self, header: ReplyHeaders) -> ProjectReplyGrant | None:
        if self._trial is not None:
            await self._trial.check()
            if header.in_reply_to != self._trial.scope.provider_header:
                return None
            header = replace(header, in_reply_to=self._trial.scope.canonical_header)
        else:
            current = await self._target()
            if current.provider_header is not None:
                if header.in_reply_to != current.provider_header and not (
                    current.provider_header in header.references
                    and header.references[-1] == header.in_reply_to
                ):
                    return None
                header = replace(header, in_reply_to=current.outbound_message_header)
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
            trial = self._trial
            expected_reply = trial.scope.provider_header if trial else (target.provider_header or target.outbound_message_header)
            header_scope = trial.scope if trial else (
                QQReplyHeaderScope(expected_reply, target.sender, target.recipient)
                if target.provider_header is not None else None
            )
            strict_mime = header_scope is not None
            if target.provider_header is not None and state.uidvalidity is None:
                raise InboundError()
            watermark = await trial.watermark() if trial else None
            if trial is not None:
                if watermark is None and (state.uidvalidity is not None or state.last_uid or state.window_end is not None):
                    raise QQTrialStopped("existing_cursor_forbidden")
                await trial.reserve("sessions")
            async with self._transport.session() as session:
                async def snapshot() -> tuple[int, int]:
                    if trial is not None:
                        await trial.check()
                    return await session.snapshot()
                validity, next_uid = await snapshot()
                if type(validity) is not int or type(next_uid) is not int or not 1 <= validity <= 4294967295 or not 1 <= next_uid <= 4294967296:
                    raise InboundError()
                if trial is not None:
                    if watermark is None:
                        await trial.initialize(QQTrialWatermark(uidvalidity=validity, high_uid=next_uid - 1))
                        next_state = QQCursor(route=self.route, attempt_id=target.attempt_id,
                            outbound_message_header=target.outbound_message_header,
                            uidvalidity=validity, last_uid=next_uid - 1)
                        return ProviderInboundPage(route=self.route, starting_cursor=cursor,
                            next_cursor=encode_cursor(next_state), items=())
                    if watermark.uidvalidity != validity or state.last_uid < watermark.high_uid:
                        raise QQTrialStopped("watermark_changed")
                if state.uidvalidity is not None and state.uidvalidity != validity:
                    raise InboundError()
                if next_uid - 1 < state.last_uid:
                    raise InboundError()
                horizon = state.window_end if state.window_end is not None else next_uid - 1
                if target.provider_header is not None:
                    horizon = min(horizon, state.last_uid + MAX_SEARCH_RESULTS)
                if horizon >= next_uid:
                    raise InboundError()
                last = state.last_uid
                items = []
                used = 0
                if trial is not None and last < horizon:
                    await trial.reserve("searches")
                wire_target = replace(target, outbound_message_header=expected_reply)
                candidates = await session.search_replies(wire_target, last, horizon) if last < horizon else ()
                if (not isinstance(candidates, tuple) or len(candidates) > MAX_SEARCH_RESULTS
                    or any(type(uid) is not int or not last < uid <= horizon for uid in candidates)
                    or tuple(sorted(set(candidates))) != candidates):
                    raise InboundError()
                if (await snapshot())[0] != validity:
                    raise InboundError()
                processed = 0
                for uid in candidates[:page_limit]:
                    if await self._target() != target:
                        raise InboundError()
                    if trial is not None:
                        await trial.reserve("headers")
                    result = await session.headers(uid)
                    if result is None:
                        processed += 1
                        last = uid
                        continue
                    if result.uid != uid or len(result.raw) > HEADER_BYTES or type(result.size) is not int or result.size <= 0:
                        raise InboundError()
                    header = reply_headers(result.raw, expected_reply=expected_reply if strict_mime else None,
                        trial_scope=header_scope)
                    if (strict_mime and header is None
                            and _associated_unsafe_headers(result.raw, expected_reply, target.recipient, target.sender, continuous=target.provider_header is not None)):
                        if trial is not None:
                            await trial.stop("reply_headers_unsafe")
                        raise QQTrialStopped("reply_headers_unsafe")
                    if (header is None or (header.in_reply_to != expected_reply and not (
                            target.provider_header is not None and expected_reply in header.references
                            and header.references[-1] == header.in_reply_to))
                        or header.sender != target.recipient or header.recipient != target.sender):
                        processed += 1
                        last = uid
                        continue
                    grant = await self._grant(header)
                    if grant != target or (await snapshot())[0] != validity:
                        raise InboundError()
                    if result.size > (min(MIME_BYTES, trial.scope.limits.body_bytes) if trial else MIME_BYTES):
                        raise InboundError()
                    if strict_mime:
                        await self._trial_mime(result.raw, result.bodystructure, result.size)
                    if used + result.size > PAGE_BYTES:
                        break
                    if trial is not None:
                        await trial.reserve("bodies")
                    raw = await session.body(uid, result.size)
                    # UID 与选定头必须一致；当前权限/发送关联撤销后不得交付归档。
                    if len(raw) != result.size or reply_headers(raw, expected_reply=expected_reply if strict_mime else None,
                            trial_scope=header_scope) != header:
                        if trial is not None:
                            await trial.stop("reply_headers_unsafe")
                            raise QQTrialStopped("reply_headers_unsafe")
                        raise InboundError()
                    if strict_mime:
                        await self._trial_mime(raw, result.bodystructure, result.size, complete=True)
                    if await self._grant(header) != grant:
                        raise InboundError()
                    current_validity, _ = await snapshot()
                    if current_validity != validity:
                        raise InboundError()
                    if trial is not None:
                        compatible = tuple(e.field for e in header.address_evidence if e.defects)
                        if compatible:
                            await trial.record_address_compatibility(compatible)
                    ref = f"qq:{validity}:{uid}"
                    with inbound_stage(InboundStage.PARSE):
                        items.append(parse_inbound_message(ref, GmailInboundRawResult(
                            status="raw", raw_mime=raw, internal_date=datetime.now(UTC),
                        ), authorized_reply_alias=(header.in_reply_to, target.outbound_message_header) if strict_mime else None))
                    used += len(raw)
                    processed += 1
                    last = uid
                    if trial is not None:
                        break
                if processed == len(candidates):
                    last = horizon
                if await self._target() != target or (await snapshot())[0] != validity:
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
