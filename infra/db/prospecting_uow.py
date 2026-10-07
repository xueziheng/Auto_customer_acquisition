"""prospecting 仓储与 outbox 的同一 SQLAlchemy 事务边界。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.repository import AccountRepository, ContactRepository
from infra.db.outbox import PostgresEventBus
from infra.db.repositories.prospecting import (
    ProspectAccountRepositoryImpl,
    ProspectContactRepositoryImpl,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import TenantId


class SqlAlchemyProspectingUnitOfWork:
    accounts: AccountRepository
    contacts: ContactRepository
    bus: EventBus

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
        self.accounts = ProspectAccountRepositoryImpl(session, self._tenant_id)
        self.contacts = ProspectContactRepositoryImpl(
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
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
