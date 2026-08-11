"""触达域事务单元：六个 repository、审计与 outbox 共用 session。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.outreach import (
    ActionRepositoryImpl,
    CampaignRepositoryImpl,
    EnrollmentRepositoryImpl,
    MessageAttemptRepositoryImpl,
    QuotaRepositoryImpl,
    SuppressionRepositoryImpl,
)
from shared.schemas.identifiers import TenantId

_cleanup_logger = logging.getLogger("infra.db.outreach.uow")


class SqlAlchemyOutreachUnitOfWork:
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
        self.campaigns = CampaignRepositoryImpl(session, self._tenant_id)
        self.enrollments = EnrollmentRepositoryImpl(session, self._tenant_id)
        self.suppressions = SuppressionRepositoryImpl(session, self._tenant_id)
        self.quotas = QuotaRepositoryImpl(session, self._tenant_id)
        self.attempts = MessageAttemptRepositoryImpl(session, self._tenant_id)
        self.actions = ActionRepositoryImpl(session, self._tenant_id)
        self.bus = PostgresEventBus(session, self._tenant_id, now=self._now)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
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
                        _cleanup_logger.error("触达事务回滚失败")
                    raise
            else:
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                    _cleanup_logger.error("触达事务回滚失败")
        finally:
            try:
                await self._session.close()
            except BaseException:
                if not preserve_primary:
                    raise
                _cleanup_logger.error("触达事务关闭失败")
