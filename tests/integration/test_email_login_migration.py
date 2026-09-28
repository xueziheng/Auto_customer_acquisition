"""邮箱登录迁移保留旧用户名，拒绝不安全降级，不截断账号。"""

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from infra.db.session import create_engine_from
from tests.integration.conftest import RedactedUrl
from tests.integration.test_migrations import _run_alembic


async def test_email_login_migration_roundtrip_preserves_accounts(integration_engine):
    name = "test_email_login_" + uuid4().hex
    async with integration_engine.connect() as connection:
        admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{name}"'))
    url = RedactedUrl(integration_engine.url.set(database=name).render_as_string(False))
    engine = create_engine_from(url)
    tenant = "ten_email_login_migration"
    address = "a" * 60 + "+qa@example.com"
    try:
        _run_alembic(url, "upgrade", "0068")
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO employees (tenant_id,employee_id,user_id,name,role,is_active) "
                "VALUES (:tenant,'emp_mail_migration','usr_mail_migration','测试','viewer',true)"
            ), {"tenant": tenant})
            await connection.execute(text(
                "INSERT INTO auth_accounts (tenant_id,username,employee_id,password_hash,enabled,version,failed_count) "
                "VALUES (:tenant,'legacy.owner','emp_mail_migration','test-hash',true,1,0)"
            ), {"tenant": tenant})
        _run_alembic(url, "upgrade", "head")
        async with engine.begin() as connection:
            for table in ("auth_accounts", "auth_sessions"):
                cols = await connection.run_sync(lambda sync, name=table: inspect(sync).get_columns(name))
                assert next(c for c in cols if c["name"] == "username")["type"].length == 254
            await connection.execute(text(
                "UPDATE auth_accounts SET username=:email WHERE tenant_id=:tenant"
            ), {"tenant": tenant, "email": address})
        for invalid in ("a..b@example.com", "UPPER@example.com", "a@-example.com", "a@example..com"):
            async with engine.connect() as connection:
                with pytest.raises(IntegrityError):
                    await connection.execute(text(
                        "UPDATE auth_accounts SET username=:email WHERE tenant_id=:tenant"
                    ), {"tenant": tenant, "email": invalid})
                await connection.rollback()
        with pytest.raises(AssertionError):
            _run_alembic(url, "downgrade", "0068")
        async with engine.begin() as connection:
            assert await connection.scalar(text(
                "SELECT username FROM auth_accounts WHERE tenant_id=:tenant"
            ), {"tenant": tenant}) == address
            await connection.execute(text(
                "UPDATE auth_accounts SET username='legacy.owner' WHERE tenant_id=:tenant"
            ), {"tenant": tenant})
        _run_alembic(url, "downgrade", "0068")
        _run_alembic(url, "upgrade", "head")
        async with engine.connect() as connection:
            assert await connection.scalar(text(
                "SELECT username FROM auth_accounts WHERE tenant_id=:tenant"
            ), {"tenant": tenant}) == "legacy.owner"
    finally:
        await engine.dispose()
        async with integration_engine.connect() as connection:
            admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{name}"'))
