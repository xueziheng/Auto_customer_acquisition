"""真实多连接 Postgres 的认证、撤销、限流与租户契约。"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.tables import EmployeeRow
from shared.schemas.identifiers import EmployeeId, TenantId, new_id


async def setup_auth(engine):
    from infra.authentication.service import PostgresAuthentication

    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    password = SecretStr(secrets.token_urlsafe(24))
    async with factory.begin() as session:
        session.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=employee,
                user_id=new_id("usr"),
                name="合成员工",
                role="sales",
                is_active=True,
            )
        )
    auth = PostgresAuthentication(factory, tenant)
    await auth.create_account("synthetic", password, employee)
    return auth, factory, tenant, employee, password


async def test_login_digest_csrf_restart_logout(integration_engine):
    from infra.authentication.service import PostgresAuthentication
    from infra.db.tables import AuthSessionRow
    from shared.authentication import AuthenticationDenied

    auth, factory, tenant, employee, password = await setup_auth(integration_engine)
    issued = await auth.login("synthetic", password)
    assert (await auth.authenticate(issued.token)).employee_id == employee
    assert (
        await auth.authenticate(issued.token, csrf_token=issued.csrf_token)
    ).employee_id == employee
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(
            issued.token, csrf_token=SecretStr(secrets.token_urlsafe(32))
        )
    restarted = PostgresAuthentication(factory, tenant)
    restored = await restarted.get_session(issued.token)
    assert restored.csrf_token == issued.csrf_token
    assert restored.expires_at == issued.expires_at
    assert (
        timedelta(hours=7, minutes=59)
        < issued.expires_at - datetime.now(UTC)
        <= timedelta(hours=8)
    )
    async with factory() as session:
        row = (
            await session.scalars(
                select(AuthSessionRow).where(AuthSessionRow.tenant_id == tenant)
            )
        ).one()
        assert row.token_digest != issued.token.get_secret_value()
        assert row.csrf_digest != issued.csrf_token.get_secret_value()
    assert issued.token.get_secret_value() not in repr(issued)
    await restarted.logout(issued.token)
    await restarted.logout(issued.token)
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(issued.token)


async def test_same_failure_account_limit_persists(integration_engine):
    from infra.authentication.service import PostgresAuthentication
    from shared.authentication import AuthenticationDenied, AuthenticationRateLimited

    auth, factory, tenant, _employee, password = await setup_auth(integration_engine)
    errors = []
    for _ in range(5):
        with pytest.raises(AuthenticationDenied) as denied:
            await auth.login("synthetic", SecretStr(secrets.token_urlsafe(24)))
        errors.append(str(denied.value))
    restarted = PostgresAuthentication(factory, tenant)
    with pytest.raises(AuthenticationRateLimited):
        await restarted.login("synthetic", password)
    with pytest.raises(AuthenticationDenied) as unknown:
        await auth.login("unknown", password)
    await auth.set_enabled("synthetic", False)
    # 独立账号避免停用状态被先前限流掩盖。
    second, _, _, _, second_password = await setup_auth(integration_engine)
    await second.set_enabled("synthetic", False)
    with pytest.raises(AuthenticationDenied) as disabled:
        await second.login("synthetic", second_password)
    assert len(set(errors + [str(unknown.value), str(disabled.value)])) == 1


async def test_unknown_buckets_bounded_and_tenant_limit(integration_engine):
    from infra.db.tables import AuthRateLimitRow
    from shared.authentication import AuthenticationDenied, AuthenticationRateLimited

    auth, factory, tenant, _, password = await setup_auth(integration_engine)
    for index in range(30):
        with pytest.raises((AuthenticationDenied, AuthenticationRateLimited)):
            await auth.login(f"unknown-{index}", password)
    with pytest.raises(AuthenticationRateLimited):
        await auth.login("synthetic", password)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuthRateLimitRow)
                .where(AuthRateLimitRow.tenant_id == tenant)
            )
            == 2
        )


async def test_expiry_cap_employee_and_tenant_fail_closed(integration_engine):
    from infra.authentication.service import PostgresAuthentication
    from infra.db.tables import AuthSessionRow
    from shared.authentication import AuthenticationDenied, AuthenticationInputInvalid

    auth, factory, tenant, employee, password = await setup_auth(integration_engine)
    issued = [await auth.login("synthetic", password) for _ in range(6)]
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(issued[0].token)
    assert (await auth.authenticate(issued[-1].token)).employee_id == employee
    other = PostgresAuthentication(factory, TenantId(new_id("tn")))
    with pytest.raises(AuthenticationDenied):
        await other.authenticate(issued[-1].token)
    with pytest.raises(AuthenticationInputInvalid):
        await other.create_account("other", password, employee)
    async with factory.begin() as session:
        await session.execute(
            update(EmployeeRow)
            .where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee)
            .values(user_id=new_id("usr"))
        )
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(issued[-1].token)
    current = await auth.login("synthetic", password)
    async with factory.begin() as session:
        await session.execute(
            update(AuthSessionRow)
            .where(AuthSessionRow.tenant_id == tenant)
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=9),
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(current.token)


@pytest.mark.parametrize("action", ["reset", "disable", "revoke"])
async def test_concurrent_account_changes_revoke_prior_sessions(
    integration_engine, action
):
    from shared.authentication import AuthenticationDenied

    auth, _, _, _, password = await setup_auth(integration_engine)
    old = await auth.login("synthetic", password)
    new_password = SecretStr(secrets.token_urlsafe(24))

    async def change():
        if action == "reset":
            await auth.reset_password("synthetic", new_password)
        elif action == "disable":
            await auth.set_enabled("synthetic", False)
        else:
            await auth.revoke_all()

    outcomes = await asyncio.gather(
        auth.login("synthetic", password), change(), return_exceptions=True
    )
    assert outcomes[1] is None
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(old.token)
    if action in ("reset", "disable") and not isinstance(outcomes[0], Exception):
        with pytest.raises(AuthenticationDenied):
            await auth.authenticate(outcomes[0].token)
    if action == "reset":
        assert await auth.authenticate(
            (await auth.login("synthetic", new_password)).token
        )


async def test_concurrent_failures_and_window_expiry(integration_engine):
    from infra.db.tables import AuthAccountRow, AuthRateLimitRow
    from shared.authentication import AuthenticationDenied, AuthenticationRateLimited

    auth, factory, tenant, _, password = await setup_auth(integration_engine)
    wrong = SecretStr(secrets.token_urlsafe(24))
    results = await asyncio.gather(
        *(auth.login("synthetic", wrong) for _ in range(8)), return_exceptions=True
    )
    assert sum(isinstance(result, AuthenticationDenied) for result in results) == 5
    assert sum(isinstance(result, AuthenticationRateLimited) for result in results) == 3
    async with factory.begin() as session:
        await session.execute(
            update(AuthAccountRow)
            .where(AuthAccountRow.tenant_id == tenant)
            .values(failure_started_at=datetime.now(UTC) - timedelta(minutes=16))
        )
        await session.execute(
            update(AuthRateLimitRow)
            .where(AuthRateLimitRow.tenant_id == tenant)
            .values(count=30, started_at=datetime.now(UTC) - timedelta(minutes=2))
        )
    assert await auth.authenticate((await auth.login("synthetic", password)).token)


async def test_atomic_account_binding_and_raw_tenant_fk(integration_engine):
    from sqlalchemy.exc import IntegrityError

    from infra.db.tables import AuthAccountRow
    from shared.authentication import AuthenticationInputInvalid

    auth, factory, tenant, employee, password = await setup_auth(integration_engine)
    next_employee = EmployeeId(new_id("emp"))
    async with factory() as session:
        transaction = await session.begin()
        session.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=next_employee,
                user_id=new_id("usr"),
                name="合成事务",
                role="sales",
                is_active=True,
            )
        )
        await auth.create_account("atomic", password, next_employee, session=session)
        await transaction.rollback()
    async with factory() as session:
        assert (
            await session.scalar(
                select(AuthAccountRow).where(
                    AuthAccountRow.tenant_id == tenant,
                    AuthAccountRow.username == "atomic",
                )
            )
            is None
        )
        with pytest.raises(AuthenticationInputInvalid):
            await auth.create_account("duplicate", password, employee)
    async with factory.begin() as session:
        session.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=next_employee,
                user_id=new_id("usr"),
                name="合成事务",
                role="sales",
                is_active=True,
            )
        )
        await auth.create_account("atomic", password, next_employee, session=session)
    assert (
        await auth.authenticate((await auth.login("atomic", password)).token)
    ).employee_id == next_employee
    async with factory.begin() as session:
        existing = await session.scalar(
            select(AuthAccountRow).where(
                AuthAccountRow.tenant_id == tenant,
                AuthAccountRow.username == "synthetic",
            )
        )
        record = SecretStr(existing.password_hash)
    try:
        async with factory.begin() as session:
            session.add(
                AuthAccountRow(
                    tenant_id=new_id("tn"),
                    username="foreign",
                    employee_id=employee,
                    password_hash=record.get_secret_value(),
                    enabled=True,
                    version=1,
                    failed_count=0,
                    failure_started_at=None,
                )
            )
    except IntegrityError:
        rejected = True
    else:
        rejected = False
    assert rejected


async def test_disable_reenable_and_tenant_revoke(integration_engine):
    from shared.authentication import AuthenticationDenied

    auth, factory, tenant, employee, password = await setup_auth(integration_engine)
    other, _, _, _, other_password = await setup_auth(integration_engine)
    issued = await auth.login("synthetic", password)
    separate = await other.login("synthetic", other_password)
    await auth.set_enabled("synthetic", False)
    await auth.set_enabled("synthetic", True)
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(issued.token)
    fresh = await auth.login("synthetic", password)
    await auth.revoke_all()
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(fresh.token)
    assert await other.authenticate(separate.token)
    fresh = await auth.login("synthetic", password)
    async with factory.begin() as session:
        await session.execute(
            update(EmployeeRow)
            .where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee)
            .values(is_active=False)
        )
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(fresh.token)


async def test_concurrent_sessions_never_exceed_cap(integration_engine):
    from infra.db.tables import AuthSessionRow
    from shared.authentication import AuthenticationDenied

    auth, factory, tenant, _, password = await setup_auth(integration_engine)
    issued = await asyncio.gather(
        *(auth.login("synthetic", password) for _ in range(8))
    )
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuthSessionRow)
            .where(
                AuthSessionRow.tenant_id == tenant, AuthSessionRow.revoked_at.is_(None)
            )
        )
    assert count == 5
    valid = 0
    for item in issued:
        try:
            await auth.authenticate(item.token)
        except AuthenticationDenied:
            continue
        valid += 1
    assert valid == 5


async def test_authentication_migration_roundtrip(db_url):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import MetaData, inspect

    from infra.db.session import create_engine_from
    from infra.db.tables import Base

    engine = create_engine_from(db_url)
    names = {"auth_accounts", "auth_sessions", "auth_rate_limits"}

    def migrate(direction, target):
        result = subprocess.run(
            [sys.executable, "scripts/run_alembic.py", direction, target],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "DATABASE_URL": str(db_url)},
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, "认证迁移失败（隐藏底层输出）"

    def inspect_schema(connection):
        inspector = inspect(connection)
        return {
            name: {
                "columns": [
                    (column["name"], str(column["type"]), column["nullable"])
                    for column in inspector.get_columns(name)
                ],
                "pk": inspector.get_pk_constraint(name),
                "fk": inspector.get_foreign_keys(name),
                "checks": inspector.get_check_constraints(name),
                "indexes": inspector.get_indexes(name),
                "unique": inspector.get_unique_constraints(name),
            }
            for name in names
        }

    def check_metadata(connection):
        def include_object(obj, name, type_, reflected, compare_to):
            return (
                name in names
                if type_ == "table"
                else getattr(getattr(obj, "table", None), "name", None) in names
            )

        metadata = MetaData()
        for table in (*sorted(names), "employees"):
            Base.metadata.tables[table].to_metadata(metadata)
        return compare_metadata(
            MigrationContext.configure(
                connection, opts={"include_object": include_object}
            ),
            metadata,
        )

    try:
        async with engine.connect() as connection:
            before = await connection.run_sync(inspect_schema)
            assert await connection.run_sync(check_metadata) == []
        migrate("downgrade", "0059")
        async with engine.connect() as connection:
            assert not (
                names
                & set(
                    await connection.run_sync(
                        lambda conn: inspect(conn).get_table_names()
                    )
                )
            )
        migrate("upgrade", "head")
        async with engine.connect() as connection:
            assert await connection.run_sync(inspect_schema) == before
            assert await connection.run_sync(check_metadata) == []
    finally:
        migrate("upgrade", "head")
        await engine.dispose()


@pytest.mark.parametrize("user_id,active", [(None, True), ("", True), (None, False)])
async def test_account_requires_active_employee_user_mapping(
    integration_engine, user_id, active
):
    from shared.authentication import AuthenticationInputInvalid

    auth, factory, tenant, _, password = await setup_auth(integration_engine)
    employee = EmployeeId(new_id("emp"))
    async with factory.begin() as session:
        session.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=employee,
                user_id=user_id,
                name="合成无映射员工",
                role="sales",
                is_active=active,
            )
        )
    with pytest.raises(AuthenticationInputInvalid):
        await auth.create_account("no-mapping", password, employee)
