"""只模拟Provider响应；不读取业务数据库，不绕过Gateway。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from connectors.gmail.inbound_transport import GmailInboundRawResult
from connectors.gmail.transport import GmailHttpStatusError

from .config import ControlledError


@dataclass(frozen=True)
class ControlledProviderCall:
    call_id: str
    operation: Literal[
        "send", "search", "profile", "inbound_list", "inbound_history", "inbound_get"
    ]
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

    def _inbound_operation(self, operation: str, values: tuple[object, ...]) -> object:
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_calls (tenant_id TEXT NOT NULL, call_id TEXT PRIMARY KEY, operation TEXT NOT NULL, recorded_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_inbound (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, ref TEXT NOT NULL, body BLOB NOT NULL, labels TEXT NOT NULL, internal_date TEXT NOT NULL, UNIQUE(tenant_id, ref))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_history_floor (tenant_id TEXT PRIMARY KEY, floor INTEGER NOT NULL)"
            )
            if operation not in {"receive", "history_floor"}:
                db.execute(
                    "INSERT INTO provider_calls VALUES (?, ?, ?, ?)",
                    (
                        self._tenant,
                        uuid4().hex,
                        operation,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                db.commit()
            if operation == "receive":
                raw, labels, stamp = values
                assert isinstance(raw, bytes)
                ref = "controlled-inbound-" + hashlib.sha256(raw).hexdigest()[:24]
                db.execute(
                    "INSERT OR IGNORE INTO provider_inbound (tenant_id, ref, body, labels, internal_date) VALUES (?, ?, ?, ?, ?)",
                    (self._tenant, ref, raw, labels, stamp),
                )
                return ref
            if operation == "history_floor":
                db.execute(
                    "INSERT INTO provider_history_floor VALUES (?, ?) ON CONFLICT(tenant_id) DO UPDATE SET floor=excluded.floor WHERE tenant_id=?",
                    (self._tenant, values[0], self._tenant),
                )
                return None
            maximum = db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) FROM provider_inbound WHERE tenant_id=?",
                (self._tenant,),
            ).fetchone()[0]
            if operation == "profile":
                return str(maximum + 1)
            if operation == "inbound_get":
                row = db.execute(
                    "SELECT length(body), labels, internal_date FROM provider_inbound WHERE tenant_id=? AND ref=?",
                    (self._tenant, values[0]),
                ).fetchone()
                if row is None:
                    return GmailInboundRawResult(status="message_gone")
                limit = values[1]
                assert isinstance(limit, int)
                if row[0] > limit:
                    return GmailInboundRawResult(status="too_large")
                content = db.execute(
                    "SELECT substr(body, 1, ?) FROM provider_inbound WHERE tenant_id=? AND ref=?",
                    (limit + 1, self._tenant, values[0]),
                ).fetchone()[0]
                return GmailInboundRawResult(
                    status="raw",
                    raw_mime=content,
                    labels=tuple(json.loads(row[1])),
                    internal_date=datetime.fromisoformat(row[2]),
                )
            boundary, page_token = values
            offset = int(str(page_token)) if page_token is not None else 0
            if operation == "inbound_list":
                rows = db.execute(
                    "SELECT ref FROM provider_inbound WHERE tenant_id=? AND internal_date>? ORDER BY ordinal LIMIT 101 OFFSET ?",
                    (
                        self._tenant,
                        datetime.fromtimestamp(int(str(boundary)), UTC).isoformat(),
                        offset,
                    ),
                ).fetchall()
            else:
                floor = db.execute(
                    "SELECT floor FROM provider_history_floor WHERE tenant_id=?",
                    (self._tenant,),
                ).fetchone()
                if floor and int(str(boundary)) < floor[0]:
                    raise GmailHttpStatusError(404)
                rows = db.execute(
                    "SELECT ref FROM provider_inbound WHERE tenant_id=? AND ordinal>=? ORDER BY ordinal LIMIT 101 OFFSET ?",
                    (self._tenant, int(str(boundary)), offset),
                ).fetchall()
            refs = tuple(row[0] for row in rows[:100])
            next_token = str(offset + 100) if len(rows) > 100 else None
            return (
                (refs, next_token, str(maximum + 1))
                if operation == "inbound_history"
                else (refs, next_token)
            )

    async def receive_inbound(
        self,
        raw_mime: bytes,
        *,
        labels: tuple[str, ...] = ("INBOX",),
        internal_date: datetime,
    ) -> str:
        """只写本owner外部Provider场景，绝不写Message/Need等业务结果。"""
        result = await asyncio.to_thread(
            self._inbound_operation,
            "receive",
            (raw_mime, json.dumps(labels), internal_date.isoformat()),
        )
        return cast(str, result)

    async def expire_inbound_history(self, before: int) -> None:
        """持久记录Provider历史过期场景，重建实例仍可复现404。"""
        await asyncio.to_thread(self._inbound_operation, "history_floor", (before,))

    async def get_profile_history_id(self, *, token: str) -> str:
        del token
        return cast(
            str, await asyncio.to_thread(self._inbound_operation, "profile", ())
        )

    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]:
        del token
        return cast(
            tuple[tuple[str, ...], str | None],
            await asyncio.to_thread(
                self._inbound_operation, "inbound_list", (after_epoch, page_token)
            ),
        )

    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]:
        del token
        return cast(
            tuple[tuple[str, ...], str | None, str],
            await asyncio.to_thread(
                self._inbound_operation,
                "inbound_history",
                (start_history_id, page_token),
            ),
        )

    async def get_inbound_message(
        self, *, token: str, message_ref: str, maximum_bytes: int
    ) -> GmailInboundRawResult:
        del token
        return cast(
            GmailInboundRawResult,
            await asyncio.to_thread(
                self._inbound_operation, "inbound_get", (message_ref, maximum_bytes)
            ),
        )

    def _read_calls(self) -> tuple[ControlledProviderCall, ...]:
        if not self._path.exists():
            return ()
        with closing(sqlite3.connect(self._path)) as db:
            rows = db.execute(
                "SELECT call_id, operation, recorded_at FROM provider_calls WHERE tenant_id=? ORDER BY rowid",
                (self._tenant,),
            ).fetchall()
        if any(
            row[1]
            not in {
                "send",
                "search",
                "profile",
                "inbound_list",
                "inbound_history",
                "inbound_get",
            }
            for row in rows
        ):
            raise ControlledError("provider_scene_invalid")
        return tuple(
            ControlledProviderCall(
                row[0],
                cast(
                    Literal[
                        "send",
                        "search",
                        "profile",
                        "inbound_list",
                        "inbound_history",
                        "inbound_get",
                    ],
                    row[1],
                ),
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
            "tradeos-controlled.example.com": b"v=spf1 -all",
            "_dmarc.tradeos-controlled.example.com": b"v=DMARC1; p=reject",
            "controlled._domainkey.tradeos-controlled.example.com": b"v=DKIM1; k=rsa; p="
            + b"QUFB" * 100,
        }
        if rdtype != "TXT" or name not in records:
            raise ControlledError("external_operation_rejected")
        return (ControlledTxt((records[name],)),)
