"""用独立测试库验证受控演练的真实运行身份；不替换角色配置或权限门禁。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from infra.db.session import create_engine_from
from infra.db.tenant_security import tenant_database_role
from scripts.controlled_web_supervisor import prepare_runtime_database
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import new_id


@dataclass(repr=False)
class RuntimeDatabases:
    admin_url: SecretStr
    tenants: tuple[str, str]
    urls: list[SecretStr]
    engines: list[AsyncEngine]


@pytest_asyncio.fixture
async def runtime_databases(
    db_url: str, integration_engine: AsyncEngine,
) -> AsyncIterator[RuntimeDatabases]:
    state = RuntimeDatabases(
        SecretStr(str(db_url)), (str(new_id("tn")), str(new_id("tn"))), [], [],
    )
    try:
        for tenant in state.tenants:
            url = await prepare_runtime_database(state.admin_url, tenant)
            state.urls.append(url)
            state.engines.append(create_engine_from(url.get_secret_value()))
        yield state
    finally:
        for engine in state.engines:
            await engine.dispose()
        async with integration_engine.begin() as connection:
            for tenant in state.tenants:
                await connection.execute(text(
                    "DELETE FROM employees WHERE tenant_id = :tenant"
                ), {"tenant": tenant})
                role = tenant_database_role(tenant)
                exists = await connection.scalar(text(
                    "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = :role"
                ), {"role": role})
                if exists:
                    await connection.execute(text(f'DROP OWNED BY "{role}"'))
                    await connection.execute(text(f'DROP ROLE "{role}"'))


async def _role_attributes(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as connection:
        return dict((await connection.execute(text("""
            SELECT current_user AS current_role, session_user AS session_role,
                   rolsuper, rolinherit, rolcreaterole, rolcreatedb,
                   rolcanlogin, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles WHERE rolname = current_user
        """))).mappings().one())


async def _insert_employee(engine: AsyncEngine, tenant: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO employees
            (tenant_id, employee_id, user_id, name, role, is_active, created_at)
            VALUES (:tenant, :employee, :user, '受控验收员工', 'sales', true, now())
        """), {
            "tenant": tenant, "employee": str(new_id("emp")), "user": str(new_id("usr")),
        })


async def test_prepared_runtime_logs_in_with_restricted_tenant_role(
    runtime_databases: RuntimeDatabases,
) -> None:
    db = runtime_databases
    for tenant, engine in zip(db.tenants, db.engines, strict=True):
        assert await _role_attributes(engine) == {
            "current_role": tenant_database_role(tenant),
            "session_role": tenant_database_role(tenant),
            "rolsuper": False, "rolinherit": False, "rolcreaterole": False,
            "rolcreatedb": False, "rolcanlogin": True,
            "rolreplication": False, "rolbypassrls": False,
        }
        with pytest.raises(DBAPIError) as denied:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TABLE forbidden_runtime_ddl (id int)"))
        assert getattr(denied.value.orig, "sqlstate", None) == "42501"


async def test_prepared_runtime_cannot_read_or_write_another_tenant(
    runtime_databases: RuntimeDatabases,
) -> None:
    db = runtime_databases
    for tenant, engine in zip(db.tenants, db.engines, strict=True):
        await _insert_employee(engine, tenant)
    for tenant, engine in zip(db.tenants, db.engines, strict=True):
        async with engine.connect() as connection:
            assert list((await connection.execute(text(
                "SELECT tenant_id FROM employees"
            ))).scalars()) == [tenant]

    a, b = db.engines
    _, other_tenant = db.tenants
    with pytest.raises(DBAPIError) as denied:
        await _insert_employee(a, other_tenant)
    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
    async with a.begin() as connection:
        changed = await connection.execute(text(
            "UPDATE employees SET name = '禁止跨企业更新' "
            "WHERE tenant_id = :tenant RETURNING employee_id"
        ), {"tenant": other_tenant})
        assert list(changed.scalars()) == []
    async with b.connect() as connection:
        assert await connection.scalar(text(
            "SELECT name FROM employees WHERE tenant_id = :tenant"
        ), {"tenant": other_tenant}) == "受控验收员工"


async def test_prepare_collision_preserves_existing_role_and_login(
    runtime_databases: RuntimeDatabases,
) -> None:
    db = runtime_databases
    existing = db.engines[0]
    before = await _role_attributes(existing)
    # 关闭旧池后重连，防止已认证连接掩盖同名角色密码被错误轮换。
    await existing.dispose()
    with pytest.raises(TenantIsolationViolation, match="运行角色已存在"):
        await prepare_runtime_database(db.admin_url, db.tenants[0])
    assert await _role_attributes(existing) == before
    await _insert_employee(existing, db.tenants[0])
