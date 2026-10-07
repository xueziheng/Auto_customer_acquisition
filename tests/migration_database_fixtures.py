"""迁移验收专用空库；复用本地测试容器，绝不降级业务夹具数据库。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from infra.db.session import create_engine_from

_ROOT = Path(__file__).resolve().parents[1]


class _MigrationDatabaseUrl(str):
    def __repr__(self) -> str:
        return "<redacted migration database URL>"


def current_migration_head() -> str:
    """读取仓库迁移图的唯一 head；实际 schema 仍由各迁移测试单独断言。"""
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise AssertionError("仓库迁移图没有 head")
    return head


async def _initialize_database(url: SecretStr) -> None:
    """仅把本次新库连接交给迁移子进程，失败不回显命令环境或日志。"""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "scripts/run_alembic.py", "upgrade", "head"],
            cwd=_ROOT,
            env={
                **os.environ,
                "DATABASE_URL": url.get_secret_value(),
                "PYTHON_DOTENV_DISABLED": "1",
            },
            capture_output=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("独立迁移测试库初始化超时") from None
    if result.returncode != 0:
        raise AssertionError("独立迁移测试库初始化失败")


@pytest_asyncio.fixture
async def migration_database_url(
    _postgres_container: PostgresContainer,
) -> AsyncIterator[str]:
    """每例创建 UUID 数据库并迁移到 head；结束只删除确认由本例创建的库。

    来源只允许当前测试进程拥有的 PostgreSQL 容器，不读取 TEST_DATABASE_URL、
    profile 或业务运行连接。数据库级隔离避免只增证据和防删除迁移相互污染。
    """
    admin_url = SecretStr(_postgres_container.get_connection_url(driver="asyncpg"))
    database_name = "tradeos_migration_test_" + uuid4().hex
    target_url = SecretStr(make_url(admin_url.get_secret_value()).set(
        database=database_name,
    ).render_as_string(False))
    admin = create_engine_from(admin_url.get_secret_value())
    created = False
    try:
        async with admin.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True
        await _initialize_database(target_url)
        yield _MigrationDatabaseUrl(target_url.get_secret_value())
    finally:
        try:
            if created:
                async with admin.connect() as connection:
                    connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                    await connection.execute(text(
                        f'DROP DATABASE "{database_name}" WITH (FORCE)'
                    ))
        finally:
            await admin.dispose()
