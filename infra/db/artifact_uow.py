"""Artifact metadata 的单 session 事务单元。"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.repositories.artifacts import (
    GeneratedArtifactRepositoryImpl,
    RawArtifactRepositoryImpl,
)
from shared.schemas.identifiers import TenantId

_cleanup_logger = logging.getLogger("infra.db.artifact.uow")


class SqlAlchemyArtifactUnitOfWork:
    """每次进入创建新 session，退出时统一提交、回滚和关闭。"""

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
        self.raw = RawArtifactRepositoryImpl(session, self._tenant_id)
        self.generated = GeneratedArtifactRepositoryImpl(session, self._tenant_id)
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
                        _cleanup_logger.error("Artifact metadata 事务回滚失败")
                    raise
            else:
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                    _cleanup_logger.error("Artifact metadata 事务回滚失败")
        finally:
            try:
                await self._session.close()
            except BaseException:
                if not preserve_primary:
                    raise
                _cleanup_logger.error("Artifact metadata 事务关闭失败")
