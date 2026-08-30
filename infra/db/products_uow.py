"""产品供给池与 Outbox 的单事务 SQLAlchemy UoW。"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.products import (
    CapabilityRepositoryImpl,
    ProductRepositoryImpl,
)
from shared.schemas.identifiers import TenantId


class SqlAlchemyProductsUnitOfWork:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        self.products = ProductRepositoryImpl(self._session, self._tenant_id)
        self.capabilities = CapabilityRepositoryImpl(self._session, self._tenant_id)
        self.bus = PostgresEventBus(self._session, self._tenant_id)
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
        except BaseException:
            await self._session.rollback()
            raise
        finally:
            await self._session.close()


__all__ = ("SqlAlchemyProductsUnitOfWork",)
