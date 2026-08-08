"""异步数据库引擎 / 会话工厂（infra 层）。

只负责根据连接串构建引擎与会话；**不记录、不打印 URL / DSN / 凭证**。
事务由调用方的 UnitOfWork 管理：``get_session`` 只负责关闭会话，异常时回滚，
正常退出**不自动 commit**。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine_from(url: str) -> AsyncEngine:
    """根据连接串创建异步引擎（asyncpg 方言）。

    连接串只由调用方或 ``DATABASE_URL`` 注入；本函数不读取、不记录、不打印。
    """
    return create_async_engine(url)


def session_factory(url: str) -> async_sessionmaker[AsyncSession]:
    """基于连接串创建会话工厂（``expire_on_commit=False``）。

    内部创建一个异步引擎并绑定；引擎生命周期由装配层负责（进程退出时 dispose）。
    本函数不记录/打印连接串。
    """
    engine = create_engine_from(url)
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@asynccontextmanager
async def get_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """事务级会话上下文：只负责关闭会话；异常时回滚，正常退出不自动 commit。

    提交与否由调用方的事务边界（UnitOfWork）决定；本函数不记录/打印 URL/DSN。
    """
    session = factory()
    try:
        yield session
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
