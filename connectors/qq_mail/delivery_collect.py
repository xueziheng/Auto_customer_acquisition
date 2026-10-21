"""受授权收件邮箱的单 UID 原件采集；不搜索、不读正文、不标记已读。"""
import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from connectors._deadline_process import DeadlineProcess, PreparationDeadline
from connectors.gmail.client import SecretResolver
from connectors.qq_mail.sent_alias import SentAliasEvidence, verify_sent_alias
from shared.schemas.qq_trial import QQTrialStopped


@dataclass(frozen=True, repr=False)
class DeliveryReadRequest:
    canonical_header: str
    idempotency_header: str
    sender: str
    recipient: str
    folder: str
    uidvalidity: int
    uid: int
    timeout_seconds: int
    header_bytes: int


class QQDeliveryAliasCollector:
    def __init__(self, resolver: SecretResolver, authorize: Callable[[], Awaitable[bool]],
                 *, deadline: PreparationDeadline | None = None) -> None:
        self._resolver, self._authorize, self._deadline = resolver, authorize, deadline

    async def collect(self, request: DeliveryReadRequest) -> SentAliasEvidence:
        if (request.folder != "INBOX"
            or type(request.uid) is not int or not 1 <= request.uid <= 4294967295
            or type(request.uidvalidity) is not int or not 1 <= request.uidvalidity <= 4294967295
            or not 1 <= request.timeout_seconds <= 60
            or not 1 <= request.header_bytes <= 16384
            or re.fullmatch(r"[a-z0-9-]{1,32}\.[0-9a-f]{64}", request.idempotency_header) is None
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
                password = self._resolver.resolve("QQ_INBOUND_AUTH")
                if not isinstance(password, str) or not 0 < len(password) <= 1024:
                    raise QQTrialStopped("alias_binding")
                await command("open")
                status, _ = await command("login", request.recipient, password)
                del password
                if status != "OK":
                    raise QQTrialStopped("alias_protocol")
                await command("limits", 32768, 16384)
                status, _ = await command("select", "INBOX")
                if status != "OK":
                    raise QQTrialStopped("alias_protocol")
                status, data = await command("status", '"INBOX"', "(UIDVALIDITY)")
                if (status != "OK" or not isinstance(data, list) or len(data) != 1
                    or not isinstance(data[0], bytes)
                    or re.findall(rb"\bUIDVALIDITY ([0-9]{1,10})\b", data[0]) != [str(request.uidvalidity).encode()]):
                    raise QQTrialStopped("alias_uidvalidity")
                await command("limits", request.header_bytes + 4096, request.header_bytes)
                fields = "MESSAGE-ID X-TRADEOS-IDEMPOTENCY-V1 FROM TO CC BCC"
                status, data = await command("uid", "FETCH", str(request.uid),
                    f"(UID BODY.PEEK[HEADER.FIELDS ({fields})])")
                literals = [item for item in data if isinstance(item, tuple)] if isinstance(data, list) else []
                if status != "OK" or len(literals) != 1 or len(literals[0]) != 2:
                    raise QQTrialStopped("alias_headers_invalid")
                meta, raw = literals[0]
                if (not isinstance(meta, bytes) or not isinstance(raw, bytes)
                    or not 0 < len(raw) <= request.header_bytes
                    or re.findall(rb"\bUID ([0-9]+)\b", meta) != [str(request.uid).encode()]
                    or re.search(rb"BODY\[HEADER\.FIELDS \(" + fields.encode() + rb"\)\] \{"
                        + str(len(raw)).encode() + rb"\}$", meta, re.IGNORECASE) is None):
                    raise QQTrialStopped("alias_headers_invalid")
                if not await self._authorize():
                    raise QQTrialStopped("alias_binding")
                proof = verify_sent_alias((raw,), source="controlled_recipient_imap",
                    canonical_header=request.canonical_header, idempotency_header=request.idempotency_header,
                    sender=request.sender, recipient=request.recipient)
                await command("close")
                deadline.remaining()
                return proof
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 不输出认证、协议或原件内容。
            raise QQTrialStopped("alias_collection_failed") from None
        finally:
            connection.close()
