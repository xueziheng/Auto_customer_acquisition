"""平台授权、固定目录、审计失败关闭与独立 HTTP 会话边界。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from domains.organization.platform_access import (
    EnterpriseDirectoryEntry,
    EnterpriseOverview,
    PlatformAccessService,
    PlatformIdentity,
)
from shared.authentication import AuthenticationDenied, AuthPrincipal, IssuedSession
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId, UserId

CONTROL = TenantId("control")
BUSINESS = TenantId("business")
PRINCIPAL = AuthPrincipal(
    tenant_id=CONTROL, employee_id=EmployeeId("platform-employee"),
    user_id=UserId("platform-user"),
)
ORIGIN = "http://127.0.0.1:18762"
TRUSTED = {"Origin": ORIGIN, "X-TradeOS-Request": "1"}


class Repository:
    def __init__(self):
        self.allowed = True
        self.audit_failure = False
        self.audits = []
        self.entries = (EnterpriseDirectoryEntry(tenant_id=BUSINESS, name="合成企业", enabled=True),)

    async def authorize(self, principal):
        if not self.allowed or principal != PRINCIPAL:
            raise PermissionDenied("平台权限不足")
        return PlatformIdentity(username="xue", display_name="平台管理员")

    async def list_enterprises(self):
        return self.entries

    async def record_overview(self, principal, enterprises):
        await self.authorize(principal)
        if self.audit_failure:
            raise RuntimeError("synthetic_audit_failure")
        self.audits.append(enterprises)


class Reader:
    def __init__(self, repository=None):
        self.calls = 0
        self.revoke = repository

    async def read(self, enterprise):
        self.calls += 1
        if self.revoke:
            self.revoke.allowed = False
        return EnterpriseOverview(
            **enterprise.model_dump(), available=True, active_members=3, admins=1,
            employees=2, customers=4, validated_needs=2, opportunities=1, active_tasks=0,
        )


async def test_foreign_principal_never_reads_enterprises():
    repo, reader = Repository(), Reader()
    service = PlatformAccessService(CONTROL, repo, {BUSINESS: reader})
    with pytest.raises(PermissionDenied):
        await service.overview(PRINCIPAL.model_copy(update={"tenant_id": BUSINESS}))
    assert reader.calls == 0 and not repo.audits


@pytest.mark.parametrize("cause", ["revocation", "audit"])
async def test_final_authorization_or_audit_failure_blocks_delivery(cause):
    repo = Repository()
    reader = Reader(repo if cause == "revocation" else None)
    repo.audit_failure = cause == "audit"
    service = PlatformAccessService(CONTROL, repo, {BUSINESS: reader})
    with pytest.raises((PermissionDenied, RuntimeError)):
        await service.overview(PRINCIPAL)
    assert reader.calls == 1 and not repo.audits


async def test_unconfigured_or_disabled_statistics_are_unknown_and_audited():
    repo = Repository()
    repo.entries += (EnterpriseDirectoryEntry(tenant_id=TenantId("disabled"), name="停用企业", enabled=False),)
    reader = Reader()
    result = await PlatformAccessService(CONTROL, repo, {TenantId("disabled"): reader}).overview(PRINCIPAL)
    assert reader.calls == 0
    assert len(repo.audits[0]) == 2
    assert all(not row.available and row.active_members is None and row.customers is None for row in result.enterprises)


async def test_cross_enterprise_reader_response_is_rejected_before_audit():
    class WrongReader:
        async def read(self, enterprise):
            return EnterpriseOverview(tenant_id=TenantId("other"), name=enterprise.name, enabled=True, available=True)

    repo = Repository()
    with pytest.raises(PermissionDenied):
        await PlatformAccessService(CONTROL, repo, {BUSINESS: WrongReader()}).overview(PRINCIPAL)
    assert not repo.audits


class Authentication:
    def __init__(self):
        self.revoked = False
        self.logouts = 0
        self.issued = IssuedSession(
            principal=PRINCIPAL, token=SecretStr("synthetic-platform-token"),
            csrf_token=SecretStr("synthetic-platform-csrf"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def login(self, username, password):
        if username != "xue" or password.get_secret_value() != "synthetic-password":
            raise AuthenticationDenied()
        self.revoked = False
        return self.issued

    async def authenticate(self, token, *, csrf_token=None):
        if (
            self.revoked or token != self.issued.token
            or (csrf_token is not None and csrf_token != self.issued.csrf_token)
        ):
            raise AuthenticationDenied()
        return PRINCIPAL

    async def get_session(self, token):
        await self.authenticate(token)
        return self.issued

    async def logout(self, token):
        assert token == self.issued.token
        self.revoked = True
        self.logouts += 1


def build_app(monkeypatch):
    import apps.api.platform_access as module

    repo, auth = Repository(), Authentication()
    engine = MagicMock(spec=AsyncEngine)
    monkeypatch.setattr(module, "PostgresPlatformAccess", lambda *_: repo)
    monkeypatch.setattr(module, "PostgresAuthentication", lambda *_: auth)
    probe = AsyncMock()
    monkeypatch.setattr(module, "assert_tenant_database_isolation", probe)
    monkeypatch.setattr(module, "assert_database_schema_current", AsyncMock())
    child = module.create_platform_app(control_tenant=CONTROL, engine=engine, origin=ORIGIN, readers=())
    app = FastAPI()
    app.mount("/api/platform", child)
    return app, child, repo, auth, probe


async def login(client):
    return await client.post(
        "/api/platform/auth/login", headers=TRUSTED,
        json={"username": "xue", "password": "synthetic-password"},
    )


async def test_platform_authentication_cookie_csrf_logout_and_no_business_routes(monkeypatch):
    app, child, repo, auth, probe = build_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (await client.get("/api/platform/overview")).status_code == 503
        async with child.router.lifespan_context(child):
            assert probe.await_count == 1
            assert (await client.get("/api/platform/overview")).status_code == 401
            response = await login(client)
            assert response.status_code == 200
            assert set(response.json()) == {"username", "display_name", "role", "csrf_token", "expires_at"}
            assert response.json()["role"] == "platform_admin"
            cookie = response.headers["set-cookie"]
            assert "tradeos_session_platform=" in cookie and "Path=/api/platform;" in cookie
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie
            assert (await client.get("/api/platform/auth/session")).status_code == 200
            result = await client.get("/api/platform/overview")
            assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
            assert len(repo.audits) == 1
            assert (await client.get("/api/platform/overview?tenant_id=other")).status_code == 400
            assert (await client.get("/api/platform/team/employees")).status_code == 404
            assert (await client.post("/api/platform/auth/logout", headers=TRUSTED)).status_code == 403
            assert not auth.revoked
            headers = {**TRUSTED, "X-CSRF-Token": response.json()["csrf_token"]}
            assert (await client.post("/api/platform/team/employees", headers=headers, json={})).status_code == 404
            assert (await client.post("/api/platform/auth/logout", headers=headers)).status_code == 204
            assert (await client.get("/api/platform/auth/session")).status_code == 401
        assert (await client.get("/api/platform/overview")).status_code == 503


async def test_non_grantee_receives_no_cookie_and_issued_session_is_revoked(monkeypatch):
    app, child, repo, auth, _ = build_app(monkeypatch)
    repo.allowed = False
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await login(client)
        assert response.status_code == 401 and "set-cookie" not in response.headers
        assert auth.revoked and auth.logouts == 1


async def test_grant_revocation_is_rechecked_for_session_and_overview(monkeypatch):
    app, child, repo, _, _ = build_app(monkeypatch)
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (await login(client)).status_code == 200
        repo.allowed = False
        assert (await client.get("/api/platform/auth/session")).status_code == 401
        assert (await client.get("/api/platform/overview")).status_code == 401
        assert not repo.audits


@pytest.mark.parametrize("headers", [
    {"Origin": "https://other.invalid", "X-TradeOS-Request": "1"},
    {"Origin": ORIGIN},
    {**TRUSTED, "X-Tenant-ID": "business"},
    {**TRUSTED, "X-Employee-ID": "boss"},
    {**TRUSTED, "Sec-Fetch-Site": "cross-site"},
])
async def test_platform_login_rejects_origin_and_identity_forgery(monkeypatch, headers):
    app, child, _, _, _ = build_app(monkeypatch)
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/platform/auth/login", headers=headers,
            json={"username": "xue", "password": "synthetic-password"},
        )
        assert response.status_code == 403 and "set-cookie" not in response.headers


async def test_enterprise_cookie_cannot_authenticate_platform(monkeypatch):
    app, child, _, _, _ = build_app(monkeypatch)
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.get(
            "/api/platform/overview", headers={"Cookie": "tradeos_session_jslt=synthetic-platform-token"},
        )
        assert response.status_code == 401


async def test_backend_errors_and_audit_failure_are_redacted(monkeypatch):
    app, child, repo, _, _ = build_app(monkeypatch)
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (await login(client)).status_code == 200
        repo.audit_failure = True
        response = await client.get("/api/platform/overview")
        assert response.status_code == 503
        assert response.json() == {"code": "platform_unavailable", "message": "平台服务暂不可用"}
        assert "synthetic_audit_failure" not in response.text


def test_platform_rejects_reused_or_control_reader_engines(monkeypatch):
    import apps.api.platform_access as module
    from infra.db.platform_access import EnterpriseReaderBinding

    engine = MagicMock(spec=AsyncEngine)
    with pytest.raises(ValueError):
        module.create_platform_app(
            control_tenant=CONTROL, engine=engine, origin=ORIGIN,
            readers=(EnterpriseReaderBinding(BUSINESS, engine),),
        )


async def test_platform_startup_failure_disposes_only_its_control_engine(monkeypatch):
    import apps.api.platform_access as module

    _, child, _, _, _ = build_app(monkeypatch)
    monkeypatch.setattr(
        module, "assert_database_schema_current",
        AsyncMock(side_effect=RuntimeError("must-not-leak-database-details")),
    )
    with pytest.raises(RuntimeError, match="^platform_database_isolation_failed$"):
        async with child.router.lifespan_context(child):
            pytest.fail("启动门禁失败不可就绪")
    assert child.state.platform_ready is False
    child.state.runtime_engine.dispose.assert_awaited_once()


async def test_all_failure_responses_are_no_store_and_schema_has_login_body(monkeypatch):
    app, child, _, _, _ = build_app(monkeypatch)
    schema = child.openapi()
    assert schema["paths"]["/auth/login"]["post"]["requestBody"]["required"] is True
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        result = await client.get("/api/platform/overview")
        assert result.status_code == 503 and result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("startup_failed", [False, True])
async def test_control_engine_close_errors_are_redacted_and_preserve_primary(monkeypatch, startup_failed):
    import apps.api.platform_access as module

    _, child, _, _, _ = build_app(monkeypatch)
    child.state.runtime_engine.dispose.side_effect = RuntimeError("private-driver-details")
    if startup_failed:
        monkeypatch.setattr(
            module, "assert_database_schema_current",
            AsyncMock(side_effect=RuntimeError("private-startup-details")),
        )
    expected = "platform_database_isolation_failed" if startup_failed else "platform_database_close_failed"
    with pytest.raises(RuntimeError, match="^" + expected + "$"):
        async with child.router.lifespan_context(child):
            assert not startup_failed
    assert child.state.platform_ready is False
