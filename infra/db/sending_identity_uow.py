"""发件身份事务单元：七个 repository 与 outbox 共用一个 AsyncSession。"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.sending_identities import (
    AuthenticationCheckRepositoryImpl,
    IdentityActionRepositoryImpl,
    ReputationRepositoryImpl,
    SendCounterRepositoryImpl,
    SendingDomainRepositoryImpl,
    SendingIdentityRepositoryImpl,
    SendReservationRepositoryImpl,
)
from shared.schemas.identifiers import TenantId


class SqlAlchemySendingIdentityUnitOfWork:
    """每次进入创建新 session；退出时统一提交/回滚并关闭。"""

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
        self.domains = SendingDomainRepositoryImpl(session, self._tenant_id)
        self.identities = SendingIdentityRepositoryImpl(session, self._tenant_id)
        self.auth_checks = AuthenticationCheckRepositoryImpl(session, self._tenant_id)
        self.reputation = ReputationRepositoryImpl(session, self._tenant_id)
        self.counters = SendCounterRepositoryImpl(session, self._tenant_id)
        self.reservations = SendReservationRepositoryImpl(session, self._tenant_id)
        self.actions = IdentityActionRepositoryImpl(session, self._tenant_id)
        self.bus = PostgresEventBus(session, self._tenant_id, now=self._now)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
