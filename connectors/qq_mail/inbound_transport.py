"""固定 QQ IMAP TLS 只读入站端口；凭证只在连接器内解析。"""
from __future__ import annotations

import asyncio
import imaplib
import re
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from connectors.gmail.client import SecretResolver
from connectors.qq_mail.inbound import (
    MAX_SEARCH_RESULTS,
    HeaderResult,
    ProjectReplyGrant,
    QQInboundSession,
)
from connectors.qq_mail.mime_structure import parse_body_fetch, parse_header_fetch
from shared.schemas.email_inbound import HEADER_BYTES, MIME_BYTES, InboundError
from shared.schemas.email_inbound_diagnostics import (
    InboundStage,
    diagnosed,
    inbound_stage,
    require_protocol_ok,
    safe_failure,
)
from shared.schemas.qq_trial import QQTrialGate, QQTrialStopped


async def _thread_call[T](call: Callable[..., T], *args: object,
    cancel_cleanup: Callable[[T], None] | None = None, **kwargs: object) -> T:
    """线程不能被Task取消；等待在途只读命令退出，避免logout与FETCH同时操作socket。"""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 保留原取消信号，完成连接清理
                break
        with suppress(Exception):
            result = task.result()
            if cancel_cleanup is not None:
                cancel_cleanup(result)
        raise


class _BoundedIMAP(imaplib.IMAP4_SSL):
    def __init__(self) -> None:
        self.budget = HEADER_BYTES * 2
        self.literal_limit = HEADER_BYTES
        super().__init__("imap.qq.com", 993, ssl_context=ssl.create_default_context(), timeout=15)

    def read(self, size: int) -> bytes:
        if size < 0 or size > self.literal_limit or size > self.budget:
            raise InboundError()
        result = super().read(size)
        self.budget -= len(result)
        return result

    def readline(self) -> bytes:
        value = self.file.readline(min(16385, self.budget + 1))
        self.budget -= len(value)
        if len(value) > 16384 or self.budget < 0:
            raise InboundError()
        return value


class _Session:
    def __init__(self, connection: _BoundedIMAP, trial_gate: QQTrialGate | None = None, *, verified_alias: bool = False):
        self.connection = connection
        self._trial = trial_gate
        self._verified_alias = verified_alias

    @diagnosed(InboundStage.UID_STATE)
    async def snapshot(self) -> tuple[int, int]:
        # 连接已 EXAMINE INBOX；禁止 SELECT 可写、STORE、MOVE、DELETE、EXPUNGE。
        def read() -> tuple[int, int]:
            self.connection.budget = HEADER_BYTES
            self.connection.literal_limit = HEADER_BYTES
            status, data = self.connection.status("INBOX", "(UIDVALIDITY UIDNEXT)")
            require_protocol_ok(status)
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], bytes):
                raise InboundError()
            values = []
            for field in (b"UIDVALIDITY", b"UIDNEXT"):
                matches = re.findall(rb"\b" + field + rb" ([0-9]{1,10})\b", data[0])
                if len(matches) != 1:
                    raise InboundError()
                values.append(int(matches[0]))
            return values[0], values[1]
        return await _thread_call(read)

    def _fetch(self, uid: int, query: str, limit: int) -> tuple[bytes, bytes] | None:
        self.connection.literal_limit = limit
        self.connection.budget = limit + HEADER_BYTES
        status, result = self.connection.uid("FETCH", str(uid), query)
        require_protocol_ok(status)
        if result == [None]:
            return None
        literals = [item for item in result if isinstance(item, tuple)]
        if len(literals) != 1 or len(literals[0]) != 2:
            raise InboundError()
        meta, raw = literals[0]
        if not isinstance(meta, bytes) or not isinstance(raw, bytes) or len(raw) > limit:
            raise InboundError()
        ids = re.findall(rb"\bUID ([0-9]+)\b", meta)
        if ids != [str(uid).encode()]:
            raise InboundError()
        return meta, raw

    @diagnosed(InboundStage.SEARCH)
    async def search_replies(
        self, target: ProjectReplyGrant, after_uid: int, before_uid: int,
    ) -> tuple[int, ...]:
        """QQ仅用回复头作有界候选查询；邮箱对与完整回复头仍在正文读取前精确核验。"""
        if self._trial is not None:
            await self._trial.check()
            scope = self._trial.scope
            if (target.route != scope.route or target.employee_id != scope.employee_id
                or target.attempt_id != scope.attempt_id
                or target.sender != scope.sender or target.recipient != scope.recipient
                or target.outbound_message_header != scope.provider_header):
                raise QQTrialStopped("scope_changed")
        # 仅受信trial可使用已核验alias；禁止引号/反斜线/控制字符进入IMAP quoted string。
        valid_header = (
            re.fullmatch(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>",
                target.outbound_message_header)
            if self._trial is not None or (self._verified_alias and target.provider_header == target.outbound_message_header) else re.fullmatch(
                r"<[a-z0-9-]{1,32}\.[0-9a-f]{64}@messages\.tradeos\.invalid>",
                target.outbound_message_header)
        )
        if (type(after_uid) is not int or type(before_uid) is not int
            or not 0 <= after_uid < before_uid <= 4294967295
            or not valid_header
            or not re.fullmatch(r"[0-9]{5,12}@qq\.com", target.sender)
            or not re.fullmatch(r"[0-9]{5,12}@qq\.com", target.recipient)):
            raise InboundError()
        def search_field(field: str) -> tuple[int, ...]:
            self.connection.budget = HEADER_BYTES
            self.connection.literal_limit = HEADER_BYTES
            status, result = self.connection.uid(
                "SEARCH", None, "UID", f"{after_uid + 1}:{before_uid}",  # type: ignore[arg-type]  # imaplib允许None表示省略charset
                "HEADER", field, '"' + target.outbound_message_header + '"',
            )
            require_protocol_ok(status)
            if (not isinstance(result, list) or len(result) != 1
                or not isinstance(result[0], bytes) or len(result[0]) > MAX_SEARCH_RESULTS * 11):
                raise InboundError()
            tokens = result[0].split()
            if len(tokens) > MAX_SEARCH_RESULTS or any(not re.fullmatch(rb"[1-9][0-9]{0,9}", t) for t in tokens):
                raise InboundError()
            uids = tuple(int(t) for t in tokens)
            if tuple(sorted(set(uids))) != uids or any(not after_uid < uid <= before_uid for uid in uids):
                raise InboundError()
            return uids

        def search() -> tuple[int, ...]:
            direct = search_field("In-Reply-To")
            if not self._verified_alias or self._trial is not None:
                return direct
            # QQ 每次只查询一个邮件头；两个结果并集仍受同一 UID 页和数量上限约束。
            threaded = search_field("References")
            combined = tuple(sorted(set(direct) | set(threaded)))
            if len(combined) > MAX_SEARCH_RESULTS:
                raise InboundError()
            return combined
        return await _thread_call(search)

    @diagnosed(InboundStage.HEADERS)
    async def headers(self, uid: int) -> HeaderResult | None:
        fields = "MESSAGE-ID IN-REPLY-TO FROM TO CC DATE"
        if self._verified_alias and self._trial is None:
            fields += " REFERENCES"
        if self._trial is not None or self._verified_alias:
            if self._trial is not None:
                await self._trial.check()
            fields += " BCC MIME-VERSION CONTENT-TYPE CONTENT-DISPOSITION CONTENT-TRANSFER-ENCODING"
            def read_trial() -> HeaderResult | None:
                self.connection.literal_limit = HEADER_BYTES
                self.connection.budget = HEADER_BYTES * 2
                status, result = self.connection.uid("FETCH", str(uid),
                    f"(UID RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS ({fields})])")
                require_protocol_ok(status)
                if result == [None]:
                    return None
                total, raw, structure = parse_header_fetch(result, uid, fields)
                return HeaderResult(uid, total, raw, structure)
            try:
                return await _thread_call(read_trial)
            except QQTrialStopped as error:
                if self._trial is not None:
                    await self._trial.stop(error.reason)
                raise
        result = await _thread_call(
            self._fetch, uid,
            f"(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({fields})])",
            HEADER_BYTES,
        )
        if result is None:
            return None
        meta, raw = result
        sizes = re.findall(rb"\bRFC822.SIZE ([0-9]+)\b", meta)
        if len(sizes) != 1:
            raise InboundError()
        return HeaderResult(uid, int(sizes[0]), raw)

    @diagnosed(InboundStage.BODY)
    async def body(self, uid: int, size: int) -> bytes:
        if not 0 < size <= MIME_BYTES:
            raise InboundError()
        if self._trial is not None or self._verified_alias:
            if self._trial is not None:
                await self._trial.check()
            def read_trial_body() -> bytes:
                self.connection.literal_limit = size
                self.connection.budget = size + HEADER_BYTES
                status, result = self.connection.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
                require_protocol_ok(status)
                return parse_body_fetch(result, uid, size)
            try:
                return await _thread_call(read_trial_body)
            except QQTrialStopped as error:
                if self._trial is not None:
                    await self._trial.stop(error.reason)
                raise
        result = await _thread_call(self._fetch, uid, "(UID BODY.PEEK[])", size)
        if result is None:
            raise InboundError()
        return result[1]


class QQImapInboundTransport:
    def __init__(self, address: str, resolver: SecretResolver, secret_ref: str,
        *, trial_gate: QQTrialGate | None = None, verified_alias: bool = False):
        if not re.fullmatch(r"[0-9]{5,12}@qq\.com", address):
            raise InboundError()
        if trial_gate is not None and address != trial_gate.scope.sender:
            raise InboundError()
        self._address, self._resolver, self._secret_ref = address, resolver, secret_ref
        self._verified_alias = verified_alias
        self._trial = trial_gate

    def __repr__(self) -> str:
        return "QQImapInboundTransport()"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[QQInboundSession]:
        connection = None
        failure = None
        try:
            if self._trial is not None:
                await self._trial.check()
            # 无模块/构造期外部 IO；Gateway 执行时才解析密钥、连接固定端点。
            with inbound_stage(InboundStage.CREDENTIAL):
                password = self._resolver.resolve(self._secret_ref)
                if not isinstance(password, str) or not password or len(password) > 1024:
                    raise InboundError()
            with inbound_stage(InboundStage.CONNECT):
                connection = await _thread_call(_BoundedIMAP, cancel_cleanup=lambda current: current.shutdown())
            with inbound_stage(InboundStage.AUTHENTICATE):
                await _thread_call(connection.login, self._address, password)
            connection.budget = HEADER_BYTES * 2
            with inbound_stage(InboundStage.SELECT) as observation:
                status, _ = await _thread_call(connection.select, "INBOX", readonly=True)
                observation.protocol(status)
                require_protocol_ok(status)
            yield _Session(connection, self._trial, verified_alias=self._verified_alias)
        except Exception as error:  # noqa: BLE001 只复制白名单字段，退出except再抛
            failure = safe_failure(InboundStage.PAGE, error, 0)
        finally:
            if connection is not None:
                with suppress(Exception):
                    await _thread_call(connection.logout)
        if failure is not None:
            raise failure from None
