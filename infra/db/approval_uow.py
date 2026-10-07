"""审批域 UoW：审批包、幂等应用与 outbox 共用同一事务。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.approvals import ApprovalRepositoryImpl
from infra.db.retryable_errors import raise_transient_database_error
from shared.schemas.identifiers import TenantId

_cleanup_logger = logging.getLogger("infra.db.approval.uow")


class SqlAlchemyApprovalUnitOfWork:
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
        self.approvals = ApprovalRepositoryImpl(session, self._tenant_id, now=self._now)
        self.bus = PostgresEventBus(session, self._tenant_id, now=self._now)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del tb
        primary = exc
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                except BaseException as commit_error:  # noqa: BLE001 - cleanup 后原样分类
                    primary = commit_error
                    try:
                        await self._session.rollback()
                    except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                        _cleanup_logger.error("审批域事务回滚失败")
            else:
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                    _cleanup_logger.error("审批域事务回滚失败")
        finally:
            try:
                await self._session.close()
            except BaseException as close_error:  # noqa: BLE001 - cleanup 后原样分类
                if primary is None:
                    primary = close_error
                else:
                    _cleanup_logger.error("审批域事务关闭失败")
        if primary is not None:
            raise_transient_database_error(primary)
            if exc is None:
                raise primary


__all__ = ("SqlAlchemyApprovalUnitOfWork",)
