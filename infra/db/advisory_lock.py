"""PostgreSQL session advisory lock 的进程级安全边界。"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId

_logger = logging.getLogger("infra.db.advisory_lock")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def derive_advisory_lock_key(tenant_id: TenantId, mailbox_alias: str) -> int:
    """以长度前缀 SHA-256 派生稳定 signed int64，避免串接歧义。"""
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or not isinstance(mailbox_alias, str)
        or not mailbox_alias
    ):
        raise ValidationError("advisory lock identity 无效")
    encoded = bytearray(b"tradeos.email-feedback-lock\x00v1\x00")
    for part in (tenant_id.encode("utf-8"), mailbox_alias.encode("utf-8")):
        encoded.extend(len(part).to_bytes(4, "big"))
        encoded.extend(part)
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=True)


class PostgresAdvisoryLock:
    """持有 dedicated connection 的 PostgreSQL session lock。"""

    def __init__(self, engine: AsyncEngine, lock_key: int) -> None:
        if (
            not isinstance(lock_key, int)
            or isinstance(lock_key, bool)
            or not _INT64_MIN <= lock_key <= _INT64_MAX
        ):
            raise ValidationError("advisory lock key 无效")
        self._engine = engine
        self._lock_key = lock_key
        self._connection: AsyncConnection | None = None
        self._owned = False
        self.backend_pid: int | None = None

    async def acquire(self) -> bool:
        """单条参数化 SELECT 取锁与 backend PID，并立即提交事务。"""
        if self._connection is not None:
            raise ValidationError("advisory lock 已初始化")
        connection = await self._engine.connect()
        self._connection = connection
        try:
            row = (
                await connection.execute(
                    text(
                        "SELECT pg_try_advisory_lock(:lock_key) AS acquired, "
                        "pg_backend_pid() AS backend_pid"
                    ),
                    {"lock_key": self._lock_key},
                )
            ).one()
            await connection.commit()
            if row.acquired is not True:
                await connection.close()
                self._connection = None
                return False
            self._owned = True
            self.backend_pid = int(row.backend_pid)
            return True
        except BaseException:
            try:
                await connection.close()
            except BaseException:  # noqa: BLE001 - cleanup 不覆盖 acquire primary
                _logger.error("advisory lock 连接清理失败")
            self._connection = None
            raise

    async def heartbeat(self) -> bool:
        """确认 dedicated physical connection 与取锁时相同。"""
        connection = self._connection
        expected_pid = self.backend_pid
        if not self._owned or connection is None or expected_pid is None:
            return False
        try:
            current_pid = (
                await connection.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            await connection.commit()
        except Exception:  # noqa: BLE001 - heartbeat 必须对驱动错误失败关闭
            try:
                await connection.rollback()
            except BaseException:  # noqa: BLE001 - close 仍负责最终回收
                _logger.error("advisory lock heartbeat 回滚失败")
            self._owned = False
            return False
        if int(current_pid) != expected_pid:
            self._owned = False
            return False
        return True

    async def close(self) -> None:
        """正常路径显式解锁并提交；连接关闭是最终兜底。"""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            if self._owned:
                try:
                    released = (
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": self._lock_key},
                        )
                    ).scalar_one()
                    await connection.commit()
                except Exception:  # noqa: BLE001 - close 仍是最终锁释放兜底
                    _logger.error("advisory lock 释放失败")
                else:
                    if released is not True:
                        _logger.error("advisory lock 释放未确认")
        finally:
            self._owned = False
            self.backend_pid = None
            await connection.close()
