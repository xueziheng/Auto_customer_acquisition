"""Tool Gateway ledger UoW：一个 request-scoped session 与 tenant-bound repository。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.repositories.tool_calls import ToolCallRepositoryImpl
from shared.schemas.identifiers import TenantId

_cleanup_logger = logging.getLogger("infra.db.tool_gateway.uow")


class SqlAlchemyToolGatewayUnitOfWork:
    """每次进入创建新 session；退出时提交或回滚，cleanup 不覆盖 primary。"""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
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
        self.calls = ToolCallRepositoryImpl(
            session,
            self._tenant_id,
            now=self._now,
        )
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
                        _cleanup_logger.error("工具账本事务回滚失败")
                    raise
            else:
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                    _cleanup_logger.error("工具账本事务回滚失败")
        finally:
            try:
                await self._session.close()
            except BaseException:
                if not preserve_primary:
                    raise
                _cleanup_logger.error("工具账本事务关闭失败")
