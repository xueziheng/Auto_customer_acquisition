"""老板指令域 UoW：提案、版本与 outbox 共享同一事务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.directives import (
    DirectiveRepositoryImpl,
    ProposalRepositoryImpl,
)
from shared.schemas.identifiers import TenantId


class SqlAlchemyDirectiveUnitOfWork:
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
        self.proposals = ProposalRepositoryImpl(session, self._tenant_id)
        self.directives = DirectiveRepositoryImpl(
            session, self._tenant_id, now=self._now
        )
        self.bus = PostgresEventBus(session, self._tenant_id, now=self._now)
        return self

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


__all__ = ("SqlAlchemyDirectiveUnitOfWork",)
