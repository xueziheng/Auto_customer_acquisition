"""真实企业数据库角色下验证独立平台授权与只读统计；只使用合成数据。"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.platform_access import create_platform_app
from domains.organization.platform_access import EnterpriseDirectoryEntry
from infra.authentication.service import PostgresAuthentication
from infra.db.platform_access import (
    EnterpriseReaderBinding,
    PostgresEnterpriseOverviewReader,
    PostgresPlatformAccess,
)
from infra.db.tables import (
    AuthAccountRow,
    AuthRateLimitRow,
    AuthSessionRow,
    EmployeeRow,
    PlatformAccessAuditRow,
    PlatformAdminGrantRow,
    PlatformEnterpriseRow,
)
from shared.authentication import AuthenticationDenied
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from tests.integration.test_tenant_row_security import (
    IsolationDatabase,
    isolated_database,  # noqa: F401 -- 显式复用独立隔离数据库 fixture
    prepared_isolation_url,  # noqa: F401 -- fixture 的 session 级依赖
)

ORIGIN = "http://127.0.0.1:18762"
TRUSTED = {"Origin": ORIGIN, "X-TradeOS-Request": "1"}


async def test_platform_real_grants_readonly_statistics_and_cross_scope_denial(
    isolated_database: IsolationDatabase,  # noqa: F811
) -> None:
    db = isolated_database
    control, business = map(TenantId, db.tenants)
    control_engine, business_engine = db.apps
    cf = async_sessionmaker(control_engine, expire_on_commit=False)
    bf = async_sessionmaker(business_engine, expire_on_commit=False)
    auth = PostgresAuthentication(cf, control)
    business_auth = PostgresAuthentication(bf, business)
    password = SecretStr(secrets.token_urlsafe(24))
    missing_tenant = TenantId(new_id("tn"))
    nongrantee = EmployeeId(new_id("emp"))
    business_boss = EmployeeId(new_id("emp"))
    read_only_checks = []

    def verify_read_only(connection, cursor, statement, parameters, context, executemany):
        del connection, parameters, context, executemany
        if statement.lstrip().upper().startswith("SELECT") and any(
            "FROM " + name in statement
            for name in ("employees", "prospect_accounts", "validated_needs", "opportunities", "workflow_runs")
        ):
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone()[0] == "on", "平台业务统计事务必须真正只读"
            read_only_checks.append(True)

    try:
        async with cf.begin() as session:
            control_user = await session.scalar(select(EmployeeRow.user_id).where(
                EmployeeRow.tenant_id == control, EmployeeRow.employee_id == db.employees[0],
            ))
            session.add(EmployeeRow(
                tenant_id=control, employee_id=nongrantee, user_id=new_id("usr"),
                name="无平台授权的老板", role="boss", is_active=True,
            ))
            session.add(PlatformAdminGrantRow(
                tenant_id=control, employee_id=db.employees[0], user_id=control_user,
                enabled=True, created_at=datetime.now(UTC),
            ))
            for target, name in ((business, "合成企业"), (missing_tenant, "未装配企业")):
                session.add(PlatformEnterpriseRow(
                    tenant_id=control, enterprise_tenant_id=target, name=name,
                    enabled=True, created_at=datetime.now(UTC),
                ))
        async with bf.begin() as session:
            session.add(EmployeeRow(
                tenant_id=business, employee_id=business_boss, user_id=new_id("usr"),
                name="合成企业管理员", role="boss", is_active=True,
            ))
            # 其他租户目录不能混入控制租户列表。
            session.add(PlatformEnterpriseRow(
                tenant_id=business, enterprise_tenant_id=missing_tenant,
                name="不得返回的外国目录", enabled=True, created_at=datetime.now(UTC),
            ))
        await auth.create_account("xue", password, EmployeeId(db.employees[0]))
        await auth.create_account("nongrantee", password, nongrantee)
        await business_auth.create_account("jslt", password, business_boss)
        business_session = await business_auth.login("jslt", password)
        child = create_platform_app(
            control_tenant=control, engine=control_engine, origin=ORIGIN,
            readers=(EnterpriseReaderBinding(business, business_engine),),
        )
        parent = FastAPI()
        parent.mount("/api/platform", child)
        async with child.router.lifespan_context(child), AsyncClient(
            transport=ASGITransport(parent), base_url=ORIGIN,
        ) as client:
            refused = await client.post(
                "/api/platform/auth/login", headers=TRUSTED,
                json={"username": "nongrantee", "password": password.get_secret_value()},
            )
            assert refused.status_code == 401 and "set-cookie" not in refused.headers
            async with cf() as session:
                assert await session.scalar(select(func.count()).select_from(AuthSessionRow).where(
                    AuthSessionRow.tenant_id == control,
                    AuthSessionRow.username == "nongrantee",
                    AuthSessionRow.revoked_at.is_(None),
                )) == 0

            # Cookie 名伪装不使另一企业的真实会话获得平台身份。
            cross = await client.get("/api/platform/overview", headers={
                "Cookie": "tradeos_session_platform=" + business_session.token.get_secret_value(),
            })
            assert cross.status_code == 401
            response = await client.post(
                "/api/platform/auth/login", headers=TRUSTED,
                json={"username": "xue", "password": password.get_secret_value()},
            )
            assert response.status_code == 200
            assert set(response.json()) == {"username", "display_name", "role", "csrf_token", "expires_at"}
            token = SecretStr(client.cookies.get("tradeos_session_platform"))
            with pytest.raises(AuthenticationDenied):
                await business_auth.authenticate(token)

            event.listen(business_engine.sync_engine, "before_cursor_execute", verify_read_only)
            result = await client.get("/api/platform/overview")
            event.remove(business_engine.sync_engine, "before_cursor_execute", verify_read_only)
            assert result.status_code == 200
            by_tenant = {row["tenant_id"]: row for row in result.json()["enterprises"]}
            assert set(by_tenant) == {business, missing_tenant}
            actual = by_tenant[business]
            assert actual["available"] is True and actual["active_members"] == 2
            assert actual["admins"] == actual["employees"] == 1
            assert actual["customers"] == actual["validated_needs"] == actual["opportunities"] == actual["active_tasks"] == 0
            assert all(set(member) == {"name", "role"} for member in actual["members"])
            assert by_tenant[missing_tenant]["customers"] is None
            assert read_only_checks and len(read_only_checks) >= 5
            async with cf() as session:
                audits = (await session.scalars(select(PlatformAccessAuditRow).where(
                    PlatformAccessAuditRow.tenant_id == control,
                ))).all()
                assert {row.target_tenant_id for row in audits} == {business, missing_tenant}
                assert all(row.actor_employee_id == db.employees[0] for row in audits)
            async with bf() as session:
                # 即使显式查询控制租户，企业角色仍由 RLS 拒绝读取平台授权。
                assert not (await session.scalars(select(PlatformAdminGrantRow).where(
                    PlatformAdminGrantRow.tenant_id == control,
                ))).all()

            assert (await client.post("/api/platform/auth/logout", headers=TRUSTED)).status_code == 403
            assert (await client.get("/api/platform/overview?tenant_id=other")).status_code == 400
            assert (await client.get("/api/platform/team/employees")).status_code == 404
            with pytest.raises(PermissionDenied):
                await PostgresEnterpriseOverviewReader(
                    EnterpriseReaderBinding(business, business_engine),
                ).read(EnterpriseDirectoryEntry(tenant_id=control, name="控制", enabled=True))
            principal = await auth.authenticate(token)
            async with cf.begin() as session:
                await session.execute(update(PlatformAdminGrantRow).where(
                    PlatformAdminGrantRow.tenant_id == control,
                    PlatformAdminGrantRow.employee_id == principal.employee_id,
                ).values(enabled=False))
            assert (await client.get("/api/platform/auth/session")).status_code == 401
            assert (await client.get("/api/platform/overview")).status_code == 401
            with pytest.raises(PermissionDenied):
                await PostgresPlatformAccess(cf, control).record_overview(principal, ())
            refused = await client.post(
                "/api/platform/auth/login", headers=TRUSTED,
                json={"username": "xue", "password": password.get_secret_value()},
            )
            assert refused.status_code == 401 and "set-cookie" not in refused.headers
            await business_auth.logout(business_session.token)
    finally:
        if event.contains(business_engine.sync_engine, "before_cursor_execute", verify_read_only):
            event.remove(business_engine.sync_engine, "before_cursor_execute", verify_read_only)
        async with db.admin.begin() as connection:
            for tenant in (control, business):
                for model in (
                    PlatformAccessAuditRow, PlatformAdminGrantRow, PlatformEnterpriseRow,
                    AuthSessionRow, AuthAccountRow, AuthRateLimitRow,
                ):
                    await connection.execute(delete(model).where(model.tenant_id == tenant))
            await connection.execute(delete(EmployeeRow).where(
                EmployeeRow.tenant_id == control, EmployeeRow.employee_id == nongrantee,
            ))
            await connection.execute(delete(EmployeeRow).where(
                EmployeeRow.tenant_id == business, EmployeeRow.employee_id == business_boss,
            ))
