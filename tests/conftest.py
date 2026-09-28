"""pytest 共享夹具：内存 SQLite 数据库 + 事务级会话（无 Docker、无业务模型）。

设计要点：
- 内存 SQLite（aiosqlite）+ StaticPool：跨连接共享同一内存库，逐测试建/删最小 schema。
- db_session 绑定连接级事务：测试结束总是回滚并关闭，fixture 生命周期确定。
- FixtureTenantRow 是纯测试用最小租户行模型（import 为 tests.conftest.FixtureTenantRow）。
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from shared.schemas.identifiers import TenantId


@pytest.fixture(scope="session", autouse=True)
def _local_http_bypasses_system_proxy() -> Iterator[None]:
    """本机测试服务直连，避免 macOS 系统代理将 loopback 请求转发为 502。"""
    exclusions = ",".join(filter(None, (
        os.environ.get("NO_PROXY"), os.environ.get("no_proxy"),
        "127.0.0.1,localhost,::1",
    )))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NO_PROXY", exclusions)
        patch.setenv("no_proxy", exclusions)
        yield


class FixtureBase(DeclarativeBase):
    """测试专用声明式基类（仅 conftest 使用）。"""


class FixtureTenantRow(FixtureBase):
    """最小租户行模型：仅用于验证夹具与租户过滤（非业务模型）。"""

    __tablename__ = "fixture_tenant_row"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """内存 SQLite 引擎（StaticPool 共享单连接）；建最小 schema，结束后删表并释放。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(FixtureBase.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(FixtureBase.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """绑定连接级事务的会话：测试结束总是回滚并关闭。"""
    connection = await db_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture
def tenant_id() -> TenantId:
    """Phase 1 单租户固定租户 ID。"""
    return TenantId("tenant_phase1")
