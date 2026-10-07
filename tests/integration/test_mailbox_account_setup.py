"""正式只读账号与邮箱绑定使用同一租户，重复设置必须验证原密码。"""

import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.tables import (
    AuthAccountRow,
    AuthRateLimitRow,
    AuthSessionRow,
    EmployeeRow,
)
from infra.pilot.mailbox_config import MailboxConfig
from shared.authentication import AuthenticationDenied
from shared.schemas.identifiers import TenantId


async def test_mailbox_setup_reuses_verified_email_login_and_binds_more_mailboxes(
    integration_engine, tmp_path,
):
    from scripts import mailbox_account

    path = tmp_path / "profile" / "config.json"
    config = MailboxConfig.create(path)
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    password = SecretStr(secrets.token_urlsafe(24))
    setup = getattr(mailbox_account, "configure_account", None)
    assert callable(setup), "正式账号创建入口尚未接线"
    try:
        await setup(
            path, sessions, username="Owner+work@Example.com", password=password,
            email="owner+work@example.com", credentials_file=Path("/private/one.json"),
        )
        await setup(
            path, sessions, username="owner+work@example.com", password=password,
            email="second@example.com", credentials_file=Path("/private/two.json"),
        )
        bindings = MailboxConfig.read(path).bindings
        assert len(bindings) == 2
        assert bindings[0].employee_id == bindings[1].employee_id
        from infra.authentication.service import PostgresAuthentication
        auth = PostgresAuthentication(sessions, TenantId(config.tenant_id))
        issued = await auth.login("OWNER+WORK@EXAMPLE.COM", password)
        assert issued.principal.employee_id == bindings[0].employee_id
        async with sessions() as db:
            role = await db.scalar(select(EmployeeRow.role).where(
                EmployeeRow.tenant_id == config.tenant_id,
                EmployeeRow.employee_id == issued.principal.employee_id,
            ))
            assert role == "viewer"
        with pytest.raises(AuthenticationDenied):
            await setup(
                path, sessions, username="owner+work@example.com",
                password=SecretStr(secrets.token_urlsafe(24)),
                email="unverified@example.com", credentials_file=Path("/private/three.json"),
            )
        assert len(MailboxConfig.read(path).bindings) == 2
    finally:
        async with sessions.begin() as db:
            for table in (AuthSessionRow, AuthRateLimitRow, AuthAccountRow, EmployeeRow):
                await db.execute(delete(table).where(table.tenant_id == config.tenant_id))
