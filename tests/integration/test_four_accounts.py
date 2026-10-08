"""四个指定账号初始化的真实事务、旧身份撤销和拒绝重入验收。"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.four_accounts import ACCOUNT_NAMES, replace_with_four_accounts
from apps.api.pilot_accounts import AccountCommand, run_account_command
from infra.authentication.service import PostgresAuthentication
from infra.db.tables import (
    AuthAccountRow,
    AuthRateLimitRow,
    AuthSessionRow,
    EmployeeRow,
    OwnershipLockRow,
    PlatformAccessAuditRow,
    PlatformAdminGrantRow,
    PlatformEnterpriseRow,
)
from shared.authentication import AuthenticationDenied, AuthenticationInputInvalid
from shared.schemas.identifiers import TenantId, new_id


def passwords():
    return {name: SecretStr("synthetic-" + name + "-password") for name in ACCOUNT_NAMES}


async def prepare(factory, tenant):
    password = SecretStr("synthetic-previous-password")
    await run_account_command(
        factory, tenant,
        AccountCommand(action="create", username="test_admin", name="旧测试管理员", role="boss"),
        password=password,
    )
    auth = PostgresAuthentication(factory, tenant)
    return auth, password, await auth.login("test_admin", password)


async def snapshot(factory, tenants):
    """只核对非秘密身份字段，不读取摘要、Token 或配置。"""
    rows = []
    async with factory() as session:
        for tenant in tenants:
            employees = (await session.execute(select(
                EmployeeRow.employee_id, EmployeeRow.user_id, EmployeeRow.role,
                EmployeeRow.is_active, EmployeeRow.manager_id,
            ).where(EmployeeRow.tenant_id == tenant).order_by(EmployeeRow.employee_id))).all()
            accounts = (await session.execute(select(
                AuthAccountRow.username, AuthAccountRow.employee_id,
                AuthAccountRow.enabled, AuthAccountRow.version,
            ).where(AuthAccountRow.tenant_id == tenant).order_by(AuthAccountRow.username))).all()
            grants = (await session.execute(select(
                PlatformAdminGrantRow.employee_id, PlatformAdminGrantRow.user_id,
                PlatformAdminGrantRow.enabled,
            ).where(PlatformAdminGrantRow.tenant_id == tenant))).all()
            directory = (await session.execute(select(
                PlatformEnterpriseRow.enterprise_tenant_id, PlatformEnterpriseRow.name,
                PlatformEnterpriseRow.enabled,
            ).where(PlatformEnterpriseRow.tenant_id == tenant))).all()
            rows.append((list(employees), list(accounts), list(grants), list(directory)))
    return rows


async def cleanup(factory, tenants):
    async with factory.begin() as session:
        for tenant in tenants:
            for model in (
                PlatformAccessAuditRow, PlatformAdminGrantRow, PlatformEnterpriseRow,
                OwnershipLockRow, AuthSessionRow, AuthAccountRow, AuthRateLimitRow, EmployeeRow,
            ):
                await session.execute(delete(model).where(model.tenant_id == tenant))


async def test_four_accounts_replace_old_login_sessions_and_employee_atomically(integration_engine):
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    business, control = TenantId(new_id("tn")), TenantId(new_id("tn"))
    tenants = (business, control)
    try:
        auth, old_password, old_session = await prepare(factory, business)
        credentials = passwords()
        result = await replace_with_four_accounts(
            factory, business_tenant=business, control_tenant=control, passwords=credentials,
        )
        by_name = {row.username: row for row in result}
        assert set(by_name) == ACCOUNT_NAMES
        assert by_name["xue"].tenant_id == control and by_name["xue"].role == "viewer"
        assert by_name["jslt"].tenant_id == business and by_name["jslt"].role == "boss"
        assert all(by_name[name].tenant_id == business and by_name[name].role == "sales" for name in ("qihao", "qikai"))
        assert len({row.employee_id for row in result}) == len({row.user_id for row in result}) == 4
        with pytest.raises(AuthenticationDenied):
            await auth.login("test_admin", old_password)
        with pytest.raises(AuthenticationDenied):
            await auth.authenticate(old_session.token)
        async with factory() as session:
            old_employee = await session.scalar(select(EmployeeRow).where(
                EmployeeRow.tenant_id == business,
                EmployeeRow.employee_id == old_session.principal.employee_id,
            ))
            assert old_employee is not None and old_employee.is_active is False
            for name in ("qihao", "qikai"):
                employee = await session.scalar(select(EmployeeRow).where(
                    EmployeeRow.tenant_id == business,
                    EmployeeRow.employee_id == by_name[name].employee_id,
                ))
                assert employee.is_active and employee.manager_id == by_name["jslt"].employee_id
            grants = (await session.scalars(select(PlatformAdminGrantRow).where(
                PlatformAdminGrantRow.tenant_id == control,
            ))).all()
            assert len(grants) == 1
            assert grants[0].employee_id == by_name["xue"].employee_id
            assert grants[0].user_id == by_name["xue"].user_id and grants[0].enabled
            assert not (await session.scalars(select(PlatformAdminGrantRow).where(
                PlatformAdminGrantRow.tenant_id == business,
            ))).all()
            live_business = (await session.scalars(select(AuthAccountRow.username).where(
                AuthAccountRow.tenant_id == business, AuthAccountRow.enabled.is_(True),
            ))).all()
            assert set(live_business) == {"jslt", "qihao", "qikai"}
        before = await snapshot(factory, tenants)
        with pytest.raises(AuthenticationInputInvalid):
            await replace_with_four_accounts(
                factory, business_tenant=business, control_tenant=control, passwords=passwords(),
            )
        assert await snapshot(factory, tenants) == before
        for name, identity in by_name.items():
            service = PostgresAuthentication(factory, identity.tenant_id)
            issued = await service.login(name, credentials[name])
            assert issued.principal.employee_id == identity.employee_id
            assert issued.principal.user_id == identity.user_id
            await service.logout(issued.token)
        with pytest.raises(AuthenticationDenied):
            await auth.login("xue", credentials["xue"])
    finally:
        await cleanup(factory, tenants)


async def test_invalid_last_password_rolls_back_all_four_accounts_and_old_revoke(integration_engine):
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    business, control = TenantId(new_id("tn")), TenantId(new_id("tn"))
    tenants = (business, control)
    try:
        auth, _, old_session = await prepare(factory, business)
        before = await snapshot(factory, tenants)
        credentials = passwords()
        credentials["qikai"] = SecretStr("")
        with pytest.raises(AuthenticationInputInvalid):
            await replace_with_four_accounts(
                factory, business_tenant=business, control_tenant=control, passwords=credentials,
            )
        assert await snapshot(factory, tenants) == before
        assert (await auth.authenticate(old_session.token)).employee_id == old_session.principal.employee_id
    finally:
        await cleanup(factory, tenants)


async def test_existing_customer_ownership_prevents_account_replacement(integration_engine):
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    business, control = TenantId(new_id("tn")), TenantId(new_id("tn"))
    tenants = (business, control)
    try:
        auth, _, old_session = await prepare(factory, business)
        async with factory.begin() as session:
            session.add(OwnershipLockRow(
                tenant_id=business, lock_id=new_id("lock"), account_id=new_id("acc"),
                owner=old_session.principal.employee_id, locked_at=datetime.now(UTC),
                locked_by_rule="synthetic-existing-customer",
            ))
        before = await snapshot(factory, tenants)
        with pytest.raises(AuthenticationInputInvalid):
            await replace_with_four_accounts(
                factory, business_tenant=business, control_tenant=control, passwords=passwords(),
            )
        assert await snapshot(factory, tenants) == before
        assert (await auth.authenticate(old_session.token)).employee_id == old_session.principal.employee_id
    finally:
        await cleanup(factory, tenants)


async def test_six_character_test_password_requires_explicit_initialization_flag(integration_engine):
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    business, control = TenantId(new_id("tn")), TenantId(new_id("tn"))
    tenants = (business, control)
    short_password = SecretStr("123456")
    try:
        auth, _, old_session = await prepare(factory, business)
        before = await snapshot(factory, tenants)
        credentials = {name: short_password for name in ACCOUNT_NAMES}
        # 普通账号命令及默认四账号初始化都保留正式密码门槛。
        with pytest.raises(AuthenticationInputInvalid):
            await run_account_command(
                factory, business,
                AccountCommand(
                    action="create", username="ordinary-short",
                    name="短密码普通账号不得创建", role="sales",
                ),
                password=short_password,
            )
        with pytest.raises(AuthenticationInputInvalid):
            await replace_with_four_accounts(
                factory, business_tenant=business, control_tenant=control,
                passwords=credentials,
            )
        assert await snapshot(factory, tenants) == before
        assert (await auth.authenticate(old_session.token)).employee_id == old_session.principal.employee_id
        result = await replace_with_four_accounts(
            factory, business_tenant=business, control_tenant=control,
            passwords=credentials, allow_test_passwords=True,
        )
        for identity in result:
            service = PostgresAuthentication(factory, identity.tenant_id)
            issued = await service.login(identity.username, short_password)
            assert issued.principal.employee_id == identity.employee_id
            # 已是测试账号，也不能把普通改密接口变成测试密码入口。
            with pytest.raises(AuthenticationInputInvalid):
                await service.reset_password(identity.username, short_password)
            assert (await service.authenticate(issued.token)).employee_id == identity.employee_id
            await service.logout(issued.token)
    finally:
        await cleanup(factory, tenants)
