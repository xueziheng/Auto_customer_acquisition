"""承诺账本与 outbox 共用同一 PostgreSQL 事务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.commitments import CommitmentRepositoryImpl
from shared.schemas.identifiers import TenantId


class SqlAlchemyCommitmentUnitOfWork:
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
        self._session = self._factory()
        self.commitments = CommitmentRepositoryImpl(self._session, self._tenant_id)
        self.bus = PostgresEventBus(self._session, self._tenant_id, now=self._now)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


__all__ = ("SqlAlchemyCommitmentUnitOfWork",)
