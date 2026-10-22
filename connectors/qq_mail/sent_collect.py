"""固定 QQ TLS 的单次 Sent 头采集；只认唯一候选，不 APPEND、不读正文。"""
import asyncio
import imaplib
import re
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from connectors._deadline_process import DeadlineProcess, PreparationDeadline
from connectors.gmail.client import SecretResolver
from connectors.qq_mail.inbound_transport import _BoundedIMAP
from connectors.qq_mail.sent_alias import SentAliasEvidence, verify_sent_alias
from shared.schemas.qq_trial import QQTrialStopped


@dataclass(frozen=True, repr=False)
class SentReadRequest:
    canonical_header: str
    idempotency_header: str
    sender: str
    recipient: str
    folder: str
    uidvalidity: int
    first_uid: int
    last_uid: int
    timeout_seconds: int
    header_bytes: int


class _SentIMAP(_BoundedIMAP):
    """限制单次停顿和字节；硬截止由拥有连接的进程外监督及 OS alarm 保证。"""
    def __init__(self, seconds: float) -> None:
        self.deadline = monotonic()+seconds
        self.budget, self.literal_limit = 32768,16384
        imaplib.IMAP4_SSL.__init__(self,"imap.qq.com",993,
            ssl_context=ssl.create_default_context(),timeout=min(15,seconds))

    def _remaining(self) -> None:
        remaining=self.deadline-monotonic()
        if remaining<=0:
            raise TimeoutError()
        self.sock.settimeout(min(15,remaining))

    def read(self,size: int) -> bytes:
        self._remaining()
        return super().read(size)

    def readline(self) -> bytes:
        self._remaining()
        return super().readline()


class QQSentAliasCollector:
    def __init__(self, resolver: SecretResolver, authorize: Callable[[], Awaitable[bool]],
                 *, deadline: PreparationDeadline | None = None) -> None:
        self._resolver, self._authorize = resolver, authorize
        self._deadline = deadline

    async def collect(self, request: SentReadRequest) -> SentAliasEvidence:
        """Gateway EXECUTING 后调用；最多一次会话、一次 SEARCH、一次必要头 FETCH。"""
        if (request.folder not in {"Sent", "Sent Messages", "&XfJT0ZAB-"}
            or not 1 <= request.first_uid <= request.last_uid <= 4294967295
            or request.last_uid-request.first_uid >= 200
            or not 1 <= request.uidvalidity <= 4294967295
            or not 1 <= request.timeout_seconds <= 60
            or not 1 <= request.header_bytes <= 16384
            or not re.fullmatch(r"[a-z0-9-]{1,32}\.[0-9a-f]{64}", request.idempotency_header)
            or request.canonical_header != f"<{request.idempotency_header}@messages.tradeos.invalid>"
            or (request.sender, request.recipient) != ("1249583590@qq.com", "986647123@qq.com")):
            raise QQTrialStopped("alias_binding")
        deadline = self._deadline or PreparationDeadline(request.timeout_seconds)
        connection = DeadlineProcess(deadline, "sent")

        async def command(operation, *args):
            deadline.remaining()
            if not await self._authorize():
                raise QQTrialStopped("alias_authorization_or_timeout")
            deadline.remaining()
            return await connection.call(operation, *args)

        try:
            async with asyncio.timeout(deadline.remaining()):
                if not await self._authorize():
                    raise QQTrialStopped("alias_binding")
                reference = "QQ_INBOUND_AUTH"
                password = self._resolver.resolve(reference)
                if not isinstance(password, str) or not 0 < len(password) <= 1024:
                    raise QQTrialStopped("alias_binding")
                await command("open")
                status, _ = await command("login", request.sender, password)
                del password
                if status != "OK":
                    raise QQTrialStopped("alias_protocol")
                await command("limits", 32768, 16384)
                status, _ = await command("select", '"'+request.folder+'"')
                if status != "OK":
                    raise QQTrialStopped("alias_protocol")
                status, data = await command("status", '"'+request.folder+'"', "(UIDVALIDITY)")
                if (status != "OK" or not isinstance(data,list) or len(data)!=1
                    or not isinstance(data[0],bytes)
                    or re.findall(rb"\bUIDVALIDITY ([0-9]{1,10})\b", data[0]) != [str(request.uidvalidity).encode()]):
                    raise QQTrialStopped("alias_uidvalidity")
                await command("limits", 4096, 16384)
                status, data = await command("uid", "SEARCH", None,
                    "UID", f"{request.first_uid}:{request.last_uid}",
                    "HEADER", "X-TradeOS-Idempotency-V1", '"'+request.idempotency_header+'"')
                if (status != "OK" or not isinstance(data,list) or len(data)!=1
                    or not isinstance(data[0],bytes) or len(data[0])>11
                    or re.fullmatch(rb"[1-9][0-9]{0,9}", data[0]) is None):
                    raise QQTrialStopped("alias_missing_or_ambiguous")
                uid = int(data[0])
                if not request.first_uid <= uid <= request.last_uid:
                    raise QQTrialStopped("alias_uid_range")
                await command("limits", request.header_bytes+4096, request.header_bytes)
                fields = "MESSAGE-ID X-TRADEOS-IDEMPOTENCY-V1 FROM TO CC BCC"
                status, data = await command("uid", "FETCH", str(uid),
                    f"(UID BODY.PEEK[HEADER.FIELDS ({fields})])")
                literals = [item for item in data if isinstance(item,tuple)] if isinstance(data,list) else []
                if status != "OK" or len(literals)!=1 or len(literals[0])!=2:
                    raise QQTrialStopped("alias_headers_invalid")
                meta, raw = literals[0]
                if (not isinstance(meta,bytes) or not isinstance(raw,bytes)
                    or not 0 < len(raw) <= request.header_bytes
                    or re.findall(rb"\bUID ([0-9]+)\b", meta) != [str(uid).encode()]
                    or re.search(rb"BODY\[HEADER\.FIELDS \("+fields.encode()+rb"\)\] \{"+str(len(raw)).encode()+rb"\}$",meta,re.IGNORECASE) is None):
                    raise QQTrialStopped("alias_headers_invalid")
                if not await self._authorize():
                    raise QQTrialStopped("alias_binding")
                evidence = verify_sent_alias((raw,), source="sender_sent",
                    canonical_header=request.canonical_header, idempotency_header=request.idempotency_header,
                    sender=request.sender, recipient=request.recipient)
                await command("close")
                deadline.remaining()
                return evidence
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 不暴露认证或协议原文
            raise QQTrialStopped("alias_collection_failed") from None
        finally:
            connection.close()
