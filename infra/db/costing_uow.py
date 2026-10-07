"""成本表与利润规则共用同一 PostgreSQL 事务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.repositories.costing import (
    CostSheetRepositoryImpl,
    MarginRuleRepositoryImpl,
)
from infra.db.repositories.costing_quote import (
    CostCoverageRepositoryImpl,
    CostingOpportunityReferenceReaderImpl,
    PriceEvidenceRepositoryImpl,
    PricingPolicyRepositoryImpl,
    QuoteFxRepositoryImpl,
)
from shared.schemas.identifiers import TenantId


class SqlAlchemyCostingUnitOfWork:
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
        self.opportunity_refs = CostingOpportunityReferenceReaderImpl(self._session, self._tenant_id)
        self.sheets = CostSheetRepositoryImpl(self._session, self._tenant_id)
        self.margin_rules = MarginRuleRepositoryImpl(
            self._session, self._tenant_id, now=self._now
        )
        self.policies = PricingPolicyRepositoryImpl(self._session, self._tenant_id)
        self.prices = PriceEvidenceRepositoryImpl(self._session, self._tenant_id)
        self.coverage = CostCoverageRepositoryImpl(self._session, self._tenant_id)
        self.quote_fx = QuoteFxRepositoryImpl(self._session, self._tenant_id)
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


__all__ = ("SqlAlchemyCostingUnitOfWork",)
