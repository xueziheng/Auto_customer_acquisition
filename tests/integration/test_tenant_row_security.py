"""独立真实 PostgreSQL 企业隔离验收；永不读取生产 profile 或自动创建 Docker。

运行者必须显式注入 TRADEOS_ISOLATION_TEST_DATABASE_URL，库名必须以
tradeos_isolation_test_ 开头。本文件只删除本次随机创建角色及测试行；
测试数据库整体由外层可信 harness 创建和回收。
"""
from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from infra.db.tenant_security import (
    _PREDICATE,
    assert_tenant_database_isolation,
    provision_tenant_role,
    tenant_database_role,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import new_id

_ROOT = Path(__file__).resolve().parents[2]


def _test_url() -> URL:
    raw = os.environ.get("TRADEOS_ISOLATION_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("未显式配置独立企业隔离测试库；不自动启动 Docker")
    try:
        url = make_url(raw)
    except Exception:  # noqa: BLE001 -- 禁止 URL 解析异常回显连接材料
        pytest.fail("隔离测试连接配置无效")
    if (
        not url.database
        or not url.database.startswith("tradeos_isolation_test_")
        or url.drivername not in {"postgresql", "postgresql+asyncpg"}
        or not url.host
    ):
        pytest.fail("只能连接指定前缀的独立 TCP PostgreSQL 测试库")
    return url.set(drivername="postgresql+asyncpg")


def _engine(url: URL) -> AsyncEngine:
    return create_async_engine(
        url, pool_size=1, max_overflow=0, hide_parameters=True,
    )


def _migrate_sync(url: URL, action: str, revision: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        result = subprocess.run(
            [sys.executable, "scripts/run_alembic.py", action, revision],
            cwd=_ROOT, env=env, capture_output=True, check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(f"隔离测试迁移超时：{action} {revision}") from None
    assert result.returncode == 0, f"隔离测试迁移失败：{action} {revision}"


async def _migrate(url: URL, action: str, revision: str) -> None:
    await asyncio.to_thread(_migrate_sync, url, action, revision)


@pytest.fixture(scope="session")
def prepared_isolation_url() -> URL:
    """全进程只准备一次 schema；初始化失败不得由后续用例反复重跑迁移。"""
    url = _test_url()
    _migrate_sync(url, "upgrade", "0070")
    return url


@dataclass(repr=False)
class IsolationDatabase:
    admin: AsyncEngine
    url: URL = field(repr=False)
    tenants: tuple[str, str]
    passwords: tuple[SecretStr, SecretStr] = field(repr=False)
    apps: tuple[AsyncEngine, AsyncEngine]
    employees: tuple[str, str]


@pytest_asyncio.fixture
async def isolated_database(
    prepared_isolation_url: URL,
) -> AsyncIterator[IsolationDatabase]:
    url = prepared_isolation_url
    admin = _engine(url)
    tenants = (str(new_id("tn")), str(new_id("tn")))
    passwords = (SecretStr(secrets.token_urlsafe(32)), SecretStr(secrets.token_urlsafe(32)))
    employees = (str(new_id("emp")), str(new_id("emp")))
    apps = tuple(
        _engine(url.set(
            username=tenant_database_role(tenant), password=password.get_secret_value(),
        ))
        for tenant, password in zip(tenants, passwords, strict=True)
    )
    state = IsolationDatabase(admin, url, tenants, passwords, apps, employees)
    try:
        async with admin.begin() as connection:
            for tenant, password in zip(tenants, passwords, strict=True):
                exists = await connection.scalar(text(
                    "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname=:role"
                ), {"role": tenant_database_role(tenant)})
                assert exists == 0, "随机测试角色冲突，拒绝覆盖"
                await provision_tenant_role(connection, tenant, password)
        for app, tenant, employee in zip(apps, tenants, employees, strict=True):
            await assert_tenant_database_isolation(app, tenant)
            async with app.begin() as connection:
                await connection.execute(text("""
                    INSERT INTO employees
                    (tenant_id, employee_id, user_id, name, role, is_active, created_at)
                    VALUES (:tenant, :employee, :user, '隔离测试员工', 'sales', true, now())
                """), {"tenant": tenant, "employee": employee, "user": str(new_id("usr"))})
        yield state
    finally:
        cleanup_failed = False
        try:
            try:
                outcomes = await asyncio.gather(
                    *(app.dispose() for app in apps), return_exceptions=True,
                )
                cleanup_failed = any(isinstance(item, BaseException) for item in outcomes)
            finally:
                try:
                    async with admin.begin() as connection:
                        for tenant in tenants:
                            await connection.execute(text(
                                "DELETE FROM employees WHERE tenant_id = :tenant"
                            ), {"tenant": tenant})
                            role = tenant_database_role(tenant)
                            exists = await connection.scalar(text(
                                "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname=:role"
                            ), {"role": role})
                            if exists:
                                await connection.execute(text(f'DROP OWNED BY "{role}"'))
                                await connection.execute(text(f'DROP ROLE "{role}"'))
                finally:
                    await admin.dispose()
        except Exception:  # noqa: BLE001 -- 清理失败不能回显数据库异常或连接材料
            raise AssertionError("隔离测试资源清理失败") from None
        if cleanup_failed:
            raise AssertionError("隔离测试连接关闭失败")


async def _visible(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(
            "SELECT tenant_id FROM employees ORDER BY employee_id"
        ))).scalars())


async def test_real_login_roles_filter_reads_writes_and_client_guc(
    isolated_database: IsolationDatabase,
) -> None:
    db = isolated_database
    a, b = db.apps
    ta, tb = db.tenants
    assert await _visible(a) == [ta]
    assert await _visible(b) == [tb]
    async with a.begin() as connection:
        await connection.execute(text(
            "SELECT set_config('tradeos.tenant_id', :tenant, true)"
        ), {"tenant": tb})
        assert list((await connection.execute(text(
            "SELECT tenant_id FROM employees"
        ))).scalars()) == [ta]
        updated = await connection.execute(text(
            "UPDATE employees SET name='仅本公司变更' RETURNING tenant_id"
        ))
        assert list(updated.scalars()) == [ta]
        removed = await connection.execute(text(
            "DELETE FROM employees WHERE tenant_id=:tenant RETURNING tenant_id"
        ), {"tenant": tb})
        assert list(removed.scalars()) == []
    async with b.connect() as connection:
        assert await connection.scalar(text("SELECT name FROM employees")) == "隔离测试员工"
    with pytest.raises(DBAPIError):
        async with a.begin() as connection:
            await connection.execute(text("""
                INSERT INTO employees
                (tenant_id, employee_id, user_id, name, role, is_active, created_at)
                VALUES (:tenant, :employee, :user, '禁止跨企业写', 'sales', true, now())
            """), {"tenant": tb, "employee": str(new_id("emp")), "user": str(new_id("usr"))})
    with pytest.raises(DBAPIError):
        async with a.begin() as connection:
            await connection.execute(text(
                "UPDATE employees SET tenant_id=:tenant"
            ), {"tenant": tb})
    async with a.begin() as connection:
        rows = await connection.execute(text("DELETE FROM employees RETURNING tenant_id"))
        assert list(rows.scalars()) == [ta]
    assert await _visible(a) == []
    assert await _visible(b) == [tb]


async def test_role_cannot_switch_truncate_disable_rls_or_delegate(
    isolated_database: IsolationDatabase,
) -> None:
    db = isolated_database
    a = db.apps[0]
    other = tenant_database_role(db.tenants[1])
    statements = (
        f'SET ROLE "{other}"',
        "TRUNCATE employees",
        "ALTER TABLE employees DISABLE ROW LEVEL SECURITY",
    )
    for statement in statements:
        with pytest.raises(DBAPIError):
            async with a.begin() as connection:
                await connection.execute(text(statement))
    async with db.admin.connect() as connection:
        before = await connection.scalar(text(
            "SELECT relacl::text FROM pg_catalog.pg_class WHERE oid='public.employees'::regclass"
        ))
    # PostgreSQL 对无 grant option 的 GRANT 可以仅警告；验收权限没有发生变化。
    async with a.begin() as connection:
        await connection.execute(text("GRANT SELECT ON employees TO PUBLIC"))
    async with db.admin.connect() as connection:
        after = await connection.scalar(text(
            "SELECT relacl::text FROM pg_catalog.pg_class WHERE oid='public.employees'::regclass"
        ))
    assert after == before
    with pytest.raises(DBAPIError):
        async with a.begin() as connection:
            await connection.execute(text("SET LOCAL row_security = off"))
            await connection.execute(text("SELECT tenant_id FROM employees"))
    assert await _visible(a) == [db.tenants[0]]


async def test_restrictive_policy_and_readiness_reject_privilege_or_policy_drift(
    isolated_database: IsolationDatabase,
) -> None:
    db = isolated_database
    a = db.apps[0]
    tenant = db.tenants[0]
    role = tenant_database_role(tenant)
    with pytest.raises(TenantIsolationViolation):
        await assert_tenant_database_isolation(db.admin, tenant)
    async with db.admin.begin() as connection:
        await connection.execute(text(
            "CREATE POLICY extra_allow_all ON employees AS PERMISSIVE "
            "FOR ALL TO PUBLIC USING (true) WITH CHECK (true)"
        ))
    try:
        assert await _visible(a) == [tenant]
        with pytest.raises(TenantIsolationViolation):
            await assert_tenant_database_isolation(a, tenant)
    finally:
        async with db.admin.begin() as connection:
            await connection.execute(text("DROP POLICY extra_allow_all ON employees"))
    async with db.admin.begin() as connection:
        await connection.execute(text(f'ALTER ROLE "{role}" BYPASSRLS'))
    try:
        with pytest.raises(TenantIsolationViolation):
            await assert_tenant_database_isolation(a, tenant)
    finally:
        async with db.admin.begin() as connection:
            await connection.execute(text(f'ALTER ROLE "{role}" NOBYPASSRLS'))
    other = tenant_database_role(db.tenants[1])
    async with db.admin.begin() as connection:
        await connection.execute(text(f'GRANT "{other}" TO "{role}"'))
    try:
        with pytest.raises(TenantIsolationViolation):
            await assert_tenant_database_isolation(a, tenant)
    finally:
        async with db.admin.begin() as connection:
            await connection.execute(text(f'REVOKE "{other}" FROM "{role}"'))
    async with db.admin.begin() as connection:
        await connection.execute(text("ALTER TABLE employees NO FORCE ROW LEVEL SECURITY"))
    try:
        with pytest.raises(TenantIsolationViolation):
            await assert_tenant_database_isolation(a, tenant)
    finally:
        async with db.admin.begin() as connection:
            await connection.execute(text("ALTER TABLE employees FORCE ROW LEVEL SECURITY"))
    async with db.admin.begin() as connection:
        await connection.execute(text("DROP POLICY tradeos_tenant_guard ON employees"))
        await connection.execute(text(
            "CREATE POLICY tradeos_tenant_guard ON employees AS RESTRICTIVE "
            "FOR ALL TO PUBLIC USING (true) WITH CHECK (true)"
        ))
    try:
        with pytest.raises(TenantIsolationViolation):
            await assert_tenant_database_isolation(a, tenant)
    finally:
        async with db.admin.begin() as connection:
            await connection.execute(text("DROP POLICY tradeos_tenant_guard ON employees"))
            await connection.execute(text(
                "CREATE POLICY tradeos_tenant_guard ON employees AS RESTRICTIVE "
                f"FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
            ))
    await assert_tenant_database_isolation(a, tenant)


async def test_independent_pooled_connections_remain_in_their_enterprise(
    isolated_database: IsolationDatabase,
) -> None:
    db = isolated_database

    async def repeat(engine: AsyncEngine, expected: str) -> None:
        for _ in range(6):
            assert await _visible(engine) == [expected]

    await asyncio.gather(*(
        repeat(engine, tenant)
        for engine, tenant in zip(db.apps, db.tenants, strict=True)
    ))


async def test_0070_roundtrip_revokes_access_while_rls_is_absent(
    isolated_database: IsolationDatabase,
) -> None:
    db = isolated_database
    for engine in db.apps:
        await engine.dispose()
    await _migrate(db.url, "downgrade", "0069")
    try:
        for engine in db.apps:
            with pytest.raises(DBAPIError):
                await _visible(engine)
        with pytest.raises(TenantIsolationViolation):
            await assert_tenant_database_isolation(db.apps[0], db.tenants[0])
    finally:
        await _migrate(db.url, "upgrade", "0070")
        async with db.admin.begin() as connection:
            for tenant, password in zip(db.tenants, db.passwords, strict=True):
                await provision_tenant_role(connection, tenant, password)
    for engine, tenant in zip(db.apps, db.tenants, strict=True):
        await assert_tenant_database_isolation(engine, tenant)
        assert await _visible(engine) == [tenant]
