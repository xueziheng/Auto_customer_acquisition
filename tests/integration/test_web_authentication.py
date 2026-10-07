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
    principal = await auth.authenticate(issued.token)
    assert principal.employee_id == employee
    csrf_principal = await auth.authenticate(issued.token, csrf_token=issued.csrf_token)
    assert csrf_principal.employee_id == employee
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(
            issued.token, csrf_token=SecretStr(secrets.token_urlsafe(32))
        )
    restarted = PostgresAuthentication(factory, tenant)
    restored = await restarted.get_session(issued.token)
    restored_csrf_matches = restored.csrf_token == issued.csrf_token
    assert restored_csrf_matches, "AUTH_RESTORED_CSRF_MISMATCH"
    expiry_unchanged = restored.expires_at == issued.expires_at
    absolute_lifetime_valid = (
        timedelta(hours=7, minutes=59)
        < issued.expires_at - datetime.now(UTC)
        <= timedelta(hours=8)
    )
    assert expiry_unchanged, "AUTH_SESSION_EXPIRY_CHANGED"
    assert absolute_lifetime_valid, "AUTH_SESSION_LIFETIME_INVALID"
    async with factory() as session:
        row = (
            await session.scalars(
                select(AuthSessionRow).where(AuthSessionRow.tenant_id == tenant)
            )
        ).one()
        session_digest_only = row.token_digest != issued.token.get_secret_value()
        csrf_digest_only = row.csrf_digest != issued.csrf_token.get_secret_value()
        assert session_digest_only, "AUTH_SESSION_PLAINTEXT_STORED"
        assert csrf_digest_only, "AUTH_CSRF_PLAINTEXT_STORED"
    session_repr_safe = issued.token.get_secret_value() not in repr(issued)
    assert session_repr_safe, "AUTH_SESSION_REPR_EXPOSED"
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
    failures_match = len(set(errors + [str(unknown.value), str(disabled.value)])) == 1
    assert failures_match, "AUTH_FAILURE_MESSAGES_DIFFER"


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
    principal = await auth.authenticate(issued[-1].token)
    assert principal.employee_id == employee
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
    change_succeeded = outcomes[1] is None
    assert change_succeeded, "AUTH_ACCOUNT_CHANGE_FAILED"
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(old.token)
    if action in ("reset", "disable") and not isinstance(outcomes[0], Exception):
        with pytest.raises(AuthenticationDenied):
            await auth.authenticate(outcomes[0].token)
    if action == "reset":
        issued = await auth.login("synthetic", new_password)
        authenticated = bool(await auth.authenticate(issued.token))
        assert authenticated, "AUTH_RESET_LOGIN_FAILED"


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
    issued = await auth.login("synthetic", password)
    authenticated = bool(await auth.authenticate(issued.token))
    assert authenticated, "AUTH_WINDOW_RECOVERY_FAILED"


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
        account_absent = (
            await session.scalar(
                select(AuthAccountRow).where(
                    AuthAccountRow.tenant_id == tenant,
                    AuthAccountRow.username == "atomic",
                )
            )
        ) is None
        assert account_absent, "AUTH_ACCOUNT_ROLLBACK_FAILED"
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
    issued = await auth.login("atomic", password)
    principal = await auth.authenticate(issued.token)
    assert principal.employee_id == next_employee
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
    other_authenticated = bool(await other.authenticate(separate.token))
    assert other_authenticated, "AUTH_OTHER_TENANT_REVOKED"
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
        migration_succeeded = result.returncode == 0
        assert migration_succeeded, "AUTH_MIGRATION_FAILED"

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


@pytest.mark.parametrize("fault", ["csrf", "password_repr", "session_repr"])
def test_authentication_failures_do_not_render_materials(tmp_path, fault):
    """真实 pytest 重写路径故障注入；子进程诊断仅用于进程内泄漏布尔检查。"""
    import base64
    import hmac
    import os
    import subprocess
    import sys
    from pathlib import Path

    raw = secrets.token_bytes(32)
    session_canary = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    csrf_canary = (
        base64.urlsafe_b64encode(hmac.digest(raw, b"tradeos:csrf:v1", "sha256"))
        .decode()
        .rstrip("=")
    )
    password_canary = secrets.token_urlsafe(24)
    wrong_csrf_canary = secrets.token_urlsafe(32)
    plugin = tmp_path / "auth_fault_plugin.py"
    plugin.write_text("""import os
import secrets
from pydantic import SecretStr
from infra.authentication import passwords
from shared.authentication import IssuedSession


def pytest_configure():
    fault = os.environ["AUTH_TEST_FAULT"]
    original_urlsafe = secrets.token_urlsafe
    if fault == "csrf":
        original_bytes = secrets.token_bytes
        secrets.token_bytes = lambda size: bytes.fromhex(os.environ["AUTH_TEST_RAW"]) if size == 32 else original_bytes(size)
        passwords.csrf_for = lambda token: SecretStr(os.environ["AUTH_TEST_WRONG_CSRF"])
    elif fault == "password_repr":
        first = True
        def password(size):
            nonlocal first
            if first:
                first = False
                return os.environ["AUTH_TEST_PASSWORD"]
            return original_urlsafe(size)
        secrets.token_urlsafe = password
        original_hash = passwords.hash_password
        class ExposedRecord(SecretStr):
            def __repr__(self):
                return os.environ["AUTH_TEST_PASSWORD"]
        passwords.hash_password = lambda password: ExposedRecord(original_hash(password).get_secret_value())
    else:
        secrets.token_urlsafe = lambda size: os.environ["AUTH_TEST_SESSION"] if size == 32 else original_urlsafe(size)
        IssuedSession.__repr__ = lambda self: self.token.get_secret_value()
""")
    root = Path(__file__).resolve().parents[2]
    targets = {
        "csrf": "tests/unit/test_authentication_passwords.py::test_csrf_derivation_and_token_encoding_are_canonical",
        "password_repr": "tests/unit/test_authentication_passwords.py::test_password_roundtrip_and_strict_records",
        "session_repr": "tests/integration/test_web_authentication.py::test_login_digest_csrf_restart_logout",
    }
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(root)]),
        "PYTEST_ADDOPTS": "",
        "AUTH_TEST_FAULT": fault,
        "AUTH_TEST_RAW": raw.hex(),
        "AUTH_TEST_PASSWORD": password_canary,
        "AUTH_TEST_SESSION": session_canary,
        "AUTH_TEST_WRONG_CSRF": wrong_csrf_canary,
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "auth_fault_plugin",
                targets[fault],
                "-q",
                "--tb=long",
                "--color=no",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("AUTH_FAULT_PROCESS_FAILED", pytrace=False)
    captured = completed.stdout + completed.stderr
    expected_guard = {
        "csrf": "AUTH_CSRF_DERIVATION_MISMATCH",
        "password_repr": "AUTH_PASSWORD_REPR_EXPOSED",
        "session_repr": "AUTH_SESSION_REPR_EXPOSED",
    }[fault]
    failed_as_expected = (
        completed.returncode == 1
        and "1 failed" in completed.stdout
        and expected_guard in captured
    )
    materials_absent = all(
        material not in captured
        for material in (
            password_canary,
            session_canary,
            csrf_canary,
            wrong_csrf_canary,
            raw.hex(),
        )
    )
    del completed, captured, env
    assert failed_as_expected, "AUTH_FAULT_NOT_EXERCISED"
    assert materials_absent, "AUTH_FAILURE_DIAGNOSTIC_LEAK"
