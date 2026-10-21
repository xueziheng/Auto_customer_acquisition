"""demand 域 UoW：信号仓储 + 事件总线（同事务）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.demand.repository import (
    DemandSignalRepository,
    NeedClusterRepository,
    NeedHypothesisRepository,
    SnapshotArtifactEvidenceRepository,
    ValidatedNeedRepository,
)
from infra.db.outbox import PostgresEventBus
from infra.db.repositories.demand import (
    DemandSignalRepositoryImpl,
    SnapshotArtifactEvidenceRepositoryImpl,
)
from infra.db.repositories.need_clusters import NeedClusterRepositoryImpl
from infra.db.repositories.need_hypotheses import (
    NeedHypothesisRepositoryImpl,
    ValidatedNeedRepositoryImpl,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import TenantId


class SqlAlchemyDemandUnitOfWork:
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
        self.signals = DemandSignalRepositoryImpl(session, self._tenant_id)
        self.snapshot_artifacts = SnapshotArtifactEvidenceRepositoryImpl(
            session, self._tenant_id
        )
        self.hypotheses = NeedHypothesisRepositoryImpl(
            session, self._tenant_id, now=self._now
        )
        self.needs = ValidatedNeedRepositoryImpl(
            session, self._tenant_id, now=self._now
        )
        self.clusters = NeedClusterRepositoryImpl(
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


class BorrowedDemandUnitOfWork:
    """上层原子操作借用的会话；退出只flush，不提交/回滚/关闭。"""

    signals: DemandSignalRepository
    snapshot_artifacts: SnapshotArtifactEvidenceRepository
    hypotheses: NeedHypothesisRepository
    needs: ValidatedNeedRepository
    clusters: NeedClusterRepository
    bus: EventBus

    def __init__(self, session: AsyncSession, tenant_id: TenantId,
                 *, now: Callable[[], datetime] | None = None) -> None:
        self._session = session
        self.signals = DemandSignalRepositoryImpl(session, tenant_id)
        self.snapshot_artifacts = SnapshotArtifactEvidenceRepositoryImpl(session, tenant_id)
        self.hypotheses = NeedHypothesisRepositoryImpl(session, tenant_id, now=now)
        self.needs = ValidatedNeedRepositoryImpl(session, tenant_id, now=now)
        self.clusters = NeedClusterRepositoryImpl(session, tenant_id, now=now)
        self.bus = PostgresEventBus(session, tenant_id, now=now)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: object) -> None:
        if exc_type is None:
            await self._session.flush()
