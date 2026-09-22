"""Catalog Product Proposal 四聚合与 Outbox 的单事务 UoW。"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.catalog_products import (
    CatalogCultivationCaseRepositoryImpl,
    CatalogEvaluationRepositoryImpl,
    CatalogPolicyRepositoryImpl,
    CatalogProductProposalRepositoryImpl,
)
from shared.schemas.identifiers import TenantId


class SqlAlchemyCatalogProductsUnitOfWork:
    """每次进入创建独立 session；四仓储和事件总线共享该事务。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        arguments = (self._session, self._tenant_id, self._factory)
        self.policies = CatalogPolicyRepositoryImpl(*arguments)
        self.evaluations = CatalogEvaluationRepositoryImpl(*arguments)
        self.proposals = CatalogProductProposalRepositoryImpl(*arguments)
        self.cultivation_cases = CatalogCultivationCaseRepositoryImpl(*arguments)
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


__all__ = ("SqlAlchemyCatalogProductsUnitOfWork",)
