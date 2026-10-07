"""为旧 runtime 验收提供真实受限角色；管理连接仅来自隔离测试库夹具。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from infra.db.session import create_engine_from
from infra.db.tenant_security import (
    assert_tenant_database_isolation,
    provision_tenant_role,
    tenant_database_role,
)

RuntimeDatabaseFactory = Callable[[str], Awaitable[str]]


class _RuntimeUrl(str):
    def __repr__(self) -> str:
        return "<redacted runtime database URL>"


@pytest_asyncio.fixture
async def runtime_database_url(db_url: str) -> AsyncIterator[RuntimeDatabaseFactory]:
    async with runtime_database_scope(db_url) as provision:
        yield provision


@asynccontextmanager
async def runtime_database_scope(db_url: str) -> AsyncIterator[RuntimeDatabaseFactory]:
    """在已迁移的测试库创建企业角色并真实验门禁，退出只回收本次角色。

    生产入口和隔离门禁均不替换；migration 测试继续使用原 db_url。
    保存真实 dispose 以免生命周期故障注入阻止测试管理连接释放。
    """
    import secrets

    admin = create_engine_from(str(db_url))
    dispose = AsyncEngine.dispose
    created: dict[str, str] = {}

    async def provision(tenant_id: str) -> str:
        if tenant_id in created:
            return created[tenant_id]
        role = tenant_database_role(tenant_id)
        password = SecretStr(secrets.token_urlsafe(32))
        async with admin.begin() as connection:
            await provision_tenant_role(connection, tenant_id, password, create_only=True)
        url = _RuntimeUrl(make_url(str(db_url)).set(
            username=role, password=password.get_secret_value(),
        ).render_as_string(hide_password=False))
        created[tenant_id] = url
        runtime = create_engine_from(url)
        try:
            await assert_tenant_database_isolation(runtime, tenant_id)
        finally:
            await dispose(runtime)
        return url

    try:
        yield provision
    finally:
        try:
            async with admin.begin() as connection:
                for tenant_id in created:
                    role = tenant_database_role(tenant_id)
                    await connection.execute(text(f'DROP OWNED BY "{role}"'))
                    await connection.execute(text(f'DROP ROLE "{role}"'))
        finally:
            await dispose(admin)
