"""集成测试夹具：session 级 Postgres 容器（testcontainers）+ alembic 迁移 + 连接串。

**不打印任何 URL / DSN / 凭证**；Docker 不可用时容器 fixture 直接 skip（作用于
依赖它的测试/夹具）。业务引擎/会话由 ``infra.db.session`` 提供——本文件不 import
它，保证 RED 阶段可正常导入。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from testcontainers.community.postgres import PostgresContainer

_CONTAINER_IMAGE = "pgvector/pgvector:pg16"
_REPO_ROOT = Path(__file__).resolve().parents[2]


class RedactedUrl(str):
    """连接串安全包装：str 行为不变（SQLAlchemy 可用真实值），repr 隐藏 DSN
    （防 pytest traceback 展示）。"""

    def __repr__(self) -> str:
        return "<redacted database URL>"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _to_asyncpg(url: str) -> str:
    """把 testcontainers 连接串转成 asyncpg 方言；未知 scheme 明确抛错（不输出 DSN）。"""
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise ValueError("未知连接串 scheme：只接受 postgresql 方言，无法转为 asyncpg")


@pytest.fixture(scope="session")
def _postgres_container() -> Iterator[PostgresContainer]:
    if not _docker_available():
        pytest.skip("Docker 不可用")
    with PostgresContainer(_CONTAINER_IMAGE) as pg:
        yield pg


@pytest.fixture(scope="session")
def _migrated(_postgres_container: PostgresContainer) -> None:
    """对容器跑 ``alembic upgrade head``（subprocess 仅经 env 注入 DATABASE_URL，
    capture_output 不输出内容，错误消息不含连接信息）。"""
    url = _to_asyncpg(_postgres_container.get_connection_url())
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        check=False,
        env=env,
        cwd=_REPO_ROOT,  # 显式仓库根，避免从其它工作目录运行失败
    )
    assert result.returncode == 0, "alembic upgrade head 失败（不输出连接内容）"


@pytest.fixture(scope="session")
def db_url(_migrated: None, _postgres_container: PostgresContainer) -> RedactedUrl:
    """容器 asyncpg 连接串（已迁移），repr 脱敏。"""
    return RedactedUrl(_to_asyncpg(_postgres_container.get_connection_url()))


@pytest_asyncio.fixture(scope="session")
async def integration_engine(db_url: RedactedUrl) -> AsyncIterator[AsyncEngine]:
    """会话级异步引擎（经 infra.db.session.create_engine_from 构建），结束后 dispose。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def integration_session(integration_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """函数级会话，绑定共享引擎；结束后关闭。"""
    session = AsyncSession(bind=integration_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
