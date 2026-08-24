"""国家政策版本、来源、激活与 durable outbox 的同一 SQLAlchemy 事务边界。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.compliance.repository import (
    CountryPolicyActivationRepository,
    CountryPolicyFieldProvenanceRepository,
    CountryPolicyVersionRepository,
)
from infra.db.outbox import PostgresEventBus
from infra.db.repositories.compliance import (
    CountryPolicyActivationRepositoryImpl,
    CountryPolicyFieldProvenanceRepositoryImpl,
    CountryPolicyVersionRepositoryImpl,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import TenantId

_cleanup_logger = logging.getLogger("infra.db.compliance.uow")


class SqlAlchemyComplianceUnitOfWork:
    """进入时绑定同一 session；正常退出原子提交，任一失败全部回滚。"""

    versions: CountryPolicyVersionRepository
    provenance: CountryPolicyFieldProvenanceRepository
    activations: CountryPolicyActivationRepository
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
        self.versions = CountryPolicyVersionRepositoryImpl(
            session, self._tenant_id
        )
        self.provenance = CountryPolicyFieldProvenanceRepositoryImpl(
            session, self._tenant_id
        )
        self.activations = CountryPolicyActivationRepositoryImpl(
            session, self._tenant_id
        )
        self.bus = PostgresEventBus(session, self._tenant_id, now=self._now)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        preserve_primary = exc_type is not None
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                except BaseException:
                    preserve_primary = True
                    try:
                        await self._session.rollback()
                    except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                        _cleanup_logger.error("合规域事务回滚失败")
                    raise
            else:
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                    _cleanup_logger.error("合规域事务回滚失败")
        finally:
            try:
                await self._session.close()
            except BaseException:
                if not preserve_primary:
                    raise
                _cleanup_logger.error("合规域事务关闭失败")


__all__ = ("SqlAlchemyComplianceUnitOfWork",)
