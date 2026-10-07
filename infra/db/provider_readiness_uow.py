"""Provider readiness 事件流的 tenant-bound SQLAlchemy 事务边界。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.repositories.provider_readiness import (
    ProviderReadinessRepositoryImpl,
)
from infra.db.retryable_errors import raise_transient_database_error
from shared.schemas.identifiers import TenantId
from tool_gateway.provider_readiness import ProviderReadinessRepository

_cleanup_logger = logging.getLogger("infra.db.provider_readiness.uow")


class SqlAlchemyProviderReadinessUnitOfWork:
    """Provider readiness 每次读写共享一个 tenant-bound 事务。"""

    readiness: ProviderReadinessRepository

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
        self.readiness = ProviderReadinessRepositoryImpl(
            session, self._tenant_id
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        primary = exc
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                except BaseException as commit_error:  # noqa: BLE001
                    primary = commit_error
                    try:
                        await self._session.rollback()
                    except BaseException:  # noqa: BLE001
                        _cleanup_logger.error(
                            "Provider readiness 事务回滚失败"
                        )
            else:
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001
                    _cleanup_logger.error(
                        "Provider readiness 事务回滚失败"
                    )
        finally:
            try:
                await self._session.close()
            except BaseException as close_error:  # noqa: BLE001
                if primary is None:
                    primary = close_error
                else:
                    _cleanup_logger.error(
                        "Provider readiness 事务关闭失败"
                    )
        if primary is not None:
            raise_transient_database_error(primary)
            if exc is None:
                raise primary


__all__ = ("SqlAlchemyProviderReadinessUnitOfWork",)
