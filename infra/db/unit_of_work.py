"""机会事务单元（UoW）：单一 AsyncSession，构造五 repo + PostgresEventBus。

``__aenter__`` 从会话工厂建一个 AsyncSession 并装配全部仓储与总线；
``__aexit__`` 无异常 commit、有异常 rollback，最后关闭会话（正确释放）。
业务写入与事件发布共享同一 session → outbox 同事务原子性（P1/P2 落地）。
"""
from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.opportunities import (
    FieldProvenanceRepositoryImpl,
    HandoffRepositoryImpl,
    LossRecordRepositoryImpl,
    OpportunityRepositoryImpl,
    ScoreSnapshotRepositoryImpl,
)
from shared.schemas.identifiers import TenantId


class SqlAlchemyOpportunityUnitOfWork:
    """``OpportunityUnitOfWork`` 的 SQLAlchemy 实现（五 repo + bus 共享同一 session）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id

    async def __aenter__(self) -> Self:
        session = self._factory()
        self._session = session
        self.opportunities = OpportunityRepositoryImpl(session, self._tenant_id)
        self.snapshots = ScoreSnapshotRepositoryImpl(session, self._tenant_id)
        self.handoffs = HandoffRepositoryImpl(session, self._tenant_id)
        self.loss_records = LossRecordRepositoryImpl(session, self._tenant_id)
        self.provenance = FieldProvenanceRepositoryImpl(session, self._tenant_id)
        self.bus = PostgresEventBus(session, self._tenant_id)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """无异常 commit；有异常 rollback；无论如何关闭 session。"""
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
