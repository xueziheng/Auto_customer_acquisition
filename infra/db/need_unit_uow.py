"""单位事实短事务：显式事务级超时，未知提交固定code且无自动重试。"""

from __future__ import annotations

import logging
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.demand.errors import NeedUnitError, NeedUnitUnavailableError
from infra.db.outbox import PostgresEventBus
from infra.db.repositories.need_units import NeedUnitRepositoryImpl
from shared.schemas.identifiers import TenantId

logger = logging.getLogger(__name__)


def _failure(exc: BaseException) -> NeedUnitUnavailableError:
    """不输出原始SQL/连接异常；锁超时与未知结果分开。"""
    if isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) in {
        "55P03",
        "57014",
    }:
        return NeedUnitUnavailableError("lock_timeout")
    return NeedUnitUnavailableError("storage_unknown")


class SqlAlchemyNeedUnitUnitOfWork:
    """原文IO发生在UoW之外，提交发生在外层授权guard之内。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        lock_timeout_ms: int,
        statement_timeout_ms: int,
    ) -> None:
        """超时是显式资源配置，无生产默认。"""
        if any(
            type(v) is not int or v <= 0
            for v in (lock_timeout_ms, statement_timeout_ms)
        ):
            raise NeedUnitError("invalid_input")
        self._factory, self._tenant = session_factory, tenant_id
        self._lock_timeout, self._statement_timeout = (
            lock_timeout_ms,
            statement_timeout_ms,
        )

    async def __aenter__(self) -> Self:
        """参数化set_config且仅对当前事务生效，不修改连接级默认。"""
        self._session = self._factory()
        try:
            await self._session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{self._lock_timeout}ms"},
            )
            await self._session.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{self._statement_timeout}ms"},
            )
            self.units = NeedUnitRepositoryImpl(self._session, self._tenant)
            self.bus = PostgresEventBus(self._session, self._tenant)
            return self
        except BaseException as exc:  # 取消也必须释放已借出连接
            await self._cleanup_entry_failure()
            if isinstance(exc, Exception):
                raise _failure(exc) from None
            raise

    async def _cleanup_entry_failure(self) -> None:
        """进入失败后连续尝试回滚与关闭，不覆盖首个异常。"""
        for cleanup in (self._session.rollback, self._session.close):
            try:
                await cleanup()
            except BaseException:  # noqa: BLE001 -- 二次清理故障不得覆盖首异常
                logger.warning("需求单位事务进入失败后的清理失败")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """异常全部回滚；提交错误不得声称没有写入。"""
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
                if isinstance(exc, SQLAlchemyError):
                    raise _failure(exc) from None
        except NeedUnitUnavailableError:
            raise
        except Exception as failure:  # noqa: BLE001 -- 提交后异常不能宣称未写入
            raise _failure(failure) from None
        finally:
            await self._close()

    async def _close(self) -> None:
        """关闭连接同样可能失去提交回包，固定未知状态而非暴露底层异常。"""
        try:
            await self._session.close()
        except Exception as exc:  # noqa: BLE001 -- 连接边界统一脱敏
            raise _failure(exc) from None
