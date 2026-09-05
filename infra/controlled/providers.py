"""只模拟Provider响应；不读取业务数据库，不绕过Gateway。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from .config import ControlledError


@dataclass(frozen=True)
class ControlledProviderCall:
    call_id: str
    operation: Literal["send", "search"]
    recorded_at: datetime


class ControlledGmailTransport:
    """owner场景持久邮箱，重建进程仍能用Provider消息头核对幂等。"""

    def __init__(self, path: Path, *, tenant_id: str) -> None:
        self._path = path
        self._tenant = tenant_id

    def _operation(self, raw: bytes | None, message_id: str, header: str) -> str | None:
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_calls (tenant_id TEXT NOT NULL, call_id TEXT PRIMARY KEY, operation TEXT NOT NULL, recorded_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO provider_calls VALUES (?, ?, ?, ?)",
                (
                    self._tenant,
                    uuid4().hex,
                    "search" if raw is None else "send",
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.commit()
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_messages (tenant_id TEXT NOT NULL, message_id TEXT, header TEXT, ref TEXT, body BLOB, PRIMARY KEY(tenant_id, message_id, header))"
            )
            row = db.execute(
                "SELECT ref, body FROM provider_messages WHERE tenant_id=? AND message_id=? AND header=?",
                (self._tenant, message_id, header),
            ).fetchone()
            if row is not None:
                if raw is not None and row[1] != raw:
                    raise ControlledError("provider_message_conflict")
                return str(row[0])
            if raw is None:
                return None
            ref = "controlled-" + hashlib.sha256(raw).hexdigest()[:24]
            db.execute(
                "INSERT INTO provider_messages VALUES (?, ?, ?, ?, ?)",
                (self._tenant, message_id, header, ref, raw),
            )
            return ref

    async def send(self, *, token: str, raw_message: bytes) -> str:
        del token
        if not isinstance(raw_message, bytes) or len(raw_message) > 4 * 1024 * 1024:
            raise ControlledError("external_operation_rejected")
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        identity, header = (
            str(message.get("Message-ID", "")),
            str(message.get("X-TradeOS-Idempotency-V1", "")),
        )
        if not identity.startswith("<") or not identity.endswith(">") or not header:
            raise ControlledError("external_operation_rejected")
        result = await asyncio.to_thread(
            self._operation, raw_message, identity[1:-1], header
        )
        if result is None:
            raise ControlledError("provider_result_unknown")
        return result

    async def search(self, *, token: str, message_id: str, header: str) -> str | None:
        del token
        return await asyncio.to_thread(self._operation, None, message_id, header)

    def _read_calls(self) -> tuple[ControlledProviderCall, ...]:
        if not self._path.exists():
            return ()
        with closing(sqlite3.connect(self._path)) as db:
            rows = db.execute(
                "SELECT call_id, operation, recorded_at FROM provider_calls WHERE tenant_id=? ORDER BY rowid",
                (self._tenant,),
            ).fetchall()
        if any(row[1] not in {"send", "search"} for row in rows):
            raise ControlledError("provider_scene_invalid")
        return tuple(
            ControlledProviderCall(
                row[0],
                cast(Literal["send", "search"], row[1]),
                datetime.fromisoformat(row[2]),
            )
            for row in rows
        )

    async def list_calls(self) -> tuple[ControlledProviderCall, ...]:
        """列出真实外部端口调用；唯一邮件数不能替代send尝试数。"""
        return await asyncio.to_thread(self._read_calls)


@dataclass(frozen=True)
class ControlledTxt:
    strings: tuple[bytes, ...]


class ControlledDnsResolver:
    """只响应明确受控域，不调用系统Resolver或公网。"""

    async def resolve(self, name: str, rdtype: str) -> tuple[ControlledTxt, ...]:
        records = {
            "tradeos-controlled.test": b"v=spf1 -all",
            "_dmarc.tradeos-controlled.test": b"v=DMARC1; p=reject",
            "controlled._domainkey.tradeos-controlled.test": b"v=DKIM1; k=rsa; p="
            + b"QUFB" * 100,
        }
        if rdtype != "TXT" or name not in records:
            raise ControlledError("external_operation_rejected")
        return (ControlledTxt((records[name],)),)
