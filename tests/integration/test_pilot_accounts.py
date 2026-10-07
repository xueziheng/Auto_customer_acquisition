"""可信本机账户命令原子性，密码仅经交互进入运行进程。"""

import secrets

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.tables import AuthAccountRow, EmployeeRow
from shared.authentication import AuthenticationDenied, AuthenticationInputInvalid
from shared.schemas.identifiers import TenantId, new_id


async def test_create_employee_account_atomic_and_password_management(
    integration_engine,
):
    from apps.api.pilot_accounts import AccountCommand, run_account_command
    from infra.authentication.service import PostgresAuthentication

    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    password = SecretStr(secrets.token_urlsafe(24))
    command = AccountCommand(
        action="create", username="synthetic", name="本机测试", role="boss"
    )
    await run_account_command(factory, tenant, command, password=password)
    auth = PostgresAuthentication(factory, tenant)
    issued = await auth.login("synthetic", password)
    with pytest.raises(AuthenticationInputInvalid):
        await run_account_command(factory, tenant, command, password=password)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EmployeeRow)
                .where(EmployeeRow.tenant_id == tenant)
            )
            == 1
        )
    for action in ("disable", "enable", "reset-password"):
        await run_account_command(
            factory,
            tenant,
            AccountCommand(action=action, username="synthetic"),
            password=password if action == "reset-password" else None,
        )
    with pytest.raises(AuthenticationDenied):
        await auth.authenticate(issued.token)
    current = await auth.login("synthetic", password)
    assert current.principal.tenant_id == tenant


@pytest.mark.parametrize(
    "fault", ["role", "foreign_manager", "sales_manager", "inactive_manager"]
)
async def test_invalid_create_rolls_back_employee_and_account(
    integration_engine, fault
):
    from apps.api.pilot_accounts import AccountCommand, run_account_command

    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    manager = new_id("emp")
    async with factory.begin() as session:
        session.add(
            EmployeeRow(
                tenant_id=new_id("tn") if fault == "foreign_manager" else tenant,
                employee_id=manager,
                user_id=new_id("usr"),
                name="经理",
                role="sales" if fault == "sales_manager" else "manager",
                is_active=fault != "inactive_manager",
            )
        )
    async with factory() as session:
        before = await session.scalar(
            select(func.count())
            .select_from(EmployeeRow)
            .where(EmployeeRow.tenant_id == tenant)
        )
    with pytest.raises(AuthenticationInputInvalid):
        command = AccountCommand(
            action="create",
            username="new-person",
            name="员工",
            role="invalid" if fault == "role" else "sales",
            manager_id=manager,
        )
        await run_account_command(
            factory, tenant, command, password=SecretStr(secrets.token_urlsafe(24))
        )
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EmployeeRow)
                .where(EmployeeRow.tenant_id == tenant)
            )
            == before
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuthAccountRow)
                .where(AuthAccountRow.tenant_id == tenant)
            )
            == 0
        )


def test_cli_requires_tty_and_rejects_password_arguments(monkeypatch, capsys):
    from apps.api.pilot_accounts import parse_command, read_password

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(AuthenticationInputInvalid):
        read_password()
    with pytest.raises(AuthenticationInputInvalid):
        parse_command(
            [
                "create",
                "--username",
                "synthetic",
                "--name",
                "员工",
                "--role",
                "sales",
                "--password",
                secrets.token_urlsafe(24),
            ]
        )
    captured = capsys.readouterr()
    empty = not captured.out and not captured.err
    assert empty, "ACCOUNT_ARGUMENTS_REFLECTED"


async def test_valid_manager_binding_and_disable_preserves_business_employee(
    integration_engine,
):
    from apps.api.pilot_accounts import AccountCommand, run_account_command
    from infra.authentication.service import PostgresAuthentication

    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    password = SecretStr(secrets.token_urlsafe(24))
    await run_account_command(
        factory,
        tenant,
        AccountCommand(
            action="create", username="manager", name="经理", role="manager"
        ),
        password=password,
    )
    auth = PostgresAuthentication(factory, tenant)
    manager = (await auth.login("manager", password)).principal.employee_id
    await run_account_command(
        factory,
        tenant,
        AccountCommand(
            action="create",
            username="sales",
            name="员工",
            role="sales",
            manager_id=manager,
        ),
        password=password,
    )
    employee = (await auth.login("sales", password)).principal.employee_id
    await run_account_command(
        factory, tenant, AccountCommand(action="disable", username="sales")
    )
    async with factory() as session:
        row = await session.scalar(
            select(EmployeeRow).where(
                EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee
            )
        )
        assert row.is_active
        assert row.manager_id == manager


def test_getpass_fallback_warning_is_not_output(monkeypatch, capsys):
    import getpass

    from apps.api.pilot_accounts import read_password

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)

    def fallback(prompt):
        import warnings

        warnings.warn("controlled_fallback", getpass.GetPassWarning)

    monkeypatch.setattr("getpass.getpass", fallback)
    with pytest.raises(AuthenticationInputInvalid):
        read_password()
    captured = capsys.readouterr()
    empty = not captured.out and not captured.err
    assert empty, "ACCOUNT_GETPASS_FALLBACK_OUTPUT"
