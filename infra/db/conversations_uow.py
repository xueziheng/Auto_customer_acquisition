"""conversations 域 UoW：分类留痕仓储 + 事件总线（同事务）。"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.conversations import ClassificationRepositoryImpl
from infra.db.tables import OutboxEventRow
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import MessageId, TenantId


class SqlAlchemyConversationsUnitOfWork:
    """每次进入创建新 session，退出时统一提交、回滚与关闭。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id
        self._now = now

    async def __aenter__(self) -> Self:
        session = self._factory()
        self._session = session
        self.classifications = ClassificationRepositoryImpl(session, self._tenant_id)
        self.bus = PostgresEventBus(session, self._tenant_id, now=self._now)
        return self

    async def lock_message(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> None:
        """同 (tenant, message) 事务级 advisory 锁：把「查重→落分类→发布事件」
        串行化——并发下只有首个事务能发布 ReplyReceived（exactly-once 由
        锁 + message 唯一约束共同保证，不依赖检查时序或单线程）。
        跨租户调用 fail closed。"""
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("跨租户数据隔离违规")
        key = zlib.crc32(f"{tenant_id}:{message_id}".encode()) & 0x7FFFFFFF
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
        )

    async def has_published_reply(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> bool:
        """该 (tenant, message) 是否已发布过 ReplyReceived（防御性守卫）。

        跨租户调用 fail closed（不能静默 false 掩盖调用错误）。"""
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("跨租户数据隔离违规")
        row = (
            await self._session.execute(
                select(OutboxEventRow.event_id)
                .where(
                    OutboxEventRow.tenant_id == str(self._tenant_id),
                    OutboxEventRow.event_type == "ReplyReceived",
                    OutboxEventRow.event_payload["message_id"].astext
                    == str(message_id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return row is not None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        session = self._session
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
