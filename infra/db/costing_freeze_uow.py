"""报价成本专用事务；超时显式、异常/取消回滚，未知提交只允许原键恢复。"""

from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.costing.errors import CostFreezeError, CostFreezeUnavailableError
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.repositories.costing_freeze import CostingFreezeRepositoryImpl
from shared.schemas.identifiers import TenantId


def _failure(exc: BaseException) -> CostFreezeUnavailableError:
    """只识别固定存储错误分类，原始异常不进入对外消息。"""
    return CostFreezeUnavailableError(
        "lock_timeout"
        if isinstance(exc, DBAPIError)
        and getattr(exc.orig, "sqlstate", None) in {"55P03", "57014"}
        else "storage_unknown"
    )


class SqlAlchemyCostingFreezeUow(SqlAlchemyCostingUnitOfWork):
    """只复用仓储装配，保留原T2事务职责与公开API。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime],
        lock_timeout_ms: int,
        statement_timeout_ms: int,
    ) -> None:
        if any(
            type(v) is not int or v <= 0
            for v in (lock_timeout_ms, statement_timeout_ms)
        ):
            raise CostFreezeError("invalid_input")
        super().__init__(session_factory, tenant_id, now=now)
        self._lock_timeout, self._statement_timeout = (
            lock_timeout_ms,
            statement_timeout_ms,
        )

    async def __aenter__(self) -> Self:
        """仅本事务set_config，连接池下一事务不继承测试或请求配置。"""
        try:
            await super().__aenter__()
            await self._session.execute(
                text("SELECT set_config('lock_timeout',:value,true)"),
                {"value": f"{self._lock_timeout}ms"},
            )
            await self._session.execute(
                text("SELECT set_config('statement_timeout',:value,true)"),
                {"value": f"{self._statement_timeout}ms"},
            )
            self.freezes = CostingFreezeRepositoryImpl(self._session, self._tenant_id)
            return self
        except BaseException as exc:
            await self._session.rollback()
            await self._session.close()
            if isinstance(exc, SQLAlchemyError):
                raise _failure(exc) from None
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """在外层context lease释放之前提交，取消也不遗留锁。"""
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                except Exception as failure:  # noqa: BLE001 -- commit回包丢失不能声称未写入
                    await self._session.rollback()
                    raise _failure(failure) from None
            else:
                await self._session.rollback()
                if isinstance(exc, SQLAlchemyError):
                    raise _failure(exc) from None
        finally:
            await self._session.close()
