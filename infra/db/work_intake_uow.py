"""员工工作上传版本链的 PostgreSQL 事务单元。"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.repositories.work_intake import WorkIntakeRepositoryImpl
from shared.schemas.identifiers import TenantId

_cleanup_logger = logging.getLogger("infra.db.work_intake.uow")


class SqlAlchemyWorkIntakeUnitOfWork:
    """每次操作使用独立 session，并在一个事务内提交状态与版本。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        self.work_intake = WorkIntakeRepositoryImpl(
            self._session,
            self._tenant_id,
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
                        _cleanup_logger.error("员工工作上传事务回滚失败")
                    raise
            else:
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                    _cleanup_logger.error("员工工作上传事务回滚失败")
        finally:
            try:
                await self._session.close()
            except BaseException:
                if not preserve_primary:
                    raise
                _cleanup_logger.error("员工工作上传事务关闭失败")


__all__ = ("SqlAlchemyWorkIntakeUnitOfWork",)
