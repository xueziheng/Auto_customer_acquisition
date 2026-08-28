"""报价专用事务边界：显式commit，失败/取消回滚且关闭连接。"""
from typing import Self
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.quotations.errors import QuotationError, QuotationUnavailableError
from infra.db.repositories.quotations import QuotationVersionRepositoryImpl
from shared.schemas.identifiers import TenantId


def _failure(exc: BaseException) -> QuotationUnavailableError:
    """只输出固定分类，异常原文、SQL与连接信息不得透出。"""
    return QuotationUnavailableError("lock_timeout" if isinstance(exc, DBAPIError)
        and getattr(exc.orig,"sqlstate",None) in {"55P03","57014"} else "storage_unknown")


class SqlAlchemyQuotationUow:
    """同session报价事务，不持有或升级外层Opportunity行锁。"""
    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId, *,
        lock_timeout_ms: int, statement_timeout_ms: int) -> None:
        """超时配置必填正数，不继承连接池的其他请求配置。"""
        if any(type(v) is not int or v<=0 for v in (lock_timeout_ms,statement_timeout_ms)):
            raise QuotationError("invalid_input")
        self._factory,self._tenant_id=factory,tenant_id
        self._lock_timeout,self._statement_timeout=lock_timeout_ms,statement_timeout_ms
        self._committed=False

    async def __aenter__(self) -> Self:
        """开始本租户事务并装配唯一session仓储。"""
        self._session=self._factory()
        try:
            await self._session.begin()
            await self._session.execute(text("SELECT set_config('lock_timeout',:value,true)"),{"value":f"{self._lock_timeout}ms"})
            await self._session.execute(text("SELECT set_config('statement_timeout',:value,true)"),{"value":f"{self._statement_timeout}ms"})
            self.quotes=QuotationVersionRepositoryImpl(self._session,self._tenant_id)
            return self
        except BaseException as exc:
            await self._cleanup()
            if isinstance(exc,SQLAlchemyError):
                raise _failure(exc) from None
            raise

    async def commit(self) -> None:
        """提交结果未知时禁止自动另键重试；调用者按原操作恢复。"""
        try:
            await self._session.commit()
            self._committed=True
        except Exception as exc:
            raise _failure(exc) from None

    async def rollback(self) -> None:
        """显式回滚本事务，清理错误也须脱敏。"""
        try:
            await self._session.rollback()
        except SQLAlchemyError as exc:
            raise _failure(exc) from None

    async def _cleanup(self) -> None:
        """包括取消在内都尽力回滚并关闭，不让池连接持锁。"""
        try:
            try:
                if not self._committed:
                    await self._session.rollback()
            finally:
                await self._session.close()
        except SQLAlchemyError as exc:
            raise _failure(exc) from None

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        """无隐式commit；正常但未提交也回滚。"""
        await self._cleanup()
        if isinstance(exc,SQLAlchemyError):
            raise _failure(exc) from None
