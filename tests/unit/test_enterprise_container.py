"""固定企业容器的真实 ASGI 边界；认证存储替身不代替 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from apps.api.dependencies import get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.schemas import EmployeeView
from shared.authentication import AuthenticationDenied, AuthPrincipal, IssuedSession
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from tests.unit.test_crm_router import _app

ORIGIN = "http://127.0.0.1:18761"
TRUSTED = {"Origin": ORIGIN, "X-TradeOS-Request": "1"}


class MemoryAuthentication:
    """只替换持久认证 IO，HTTP 认证、CSRF 和当前员工判权保持真实实现。"""

    def __init__(self, tenant: str) -> None:
        self.principal = AuthPrincipal(
            tenant_id=TenantId(tenant),
            employee_id=EmployeeId("same-employee"),
            user_id=UserId("same-user"),
        )
        self.sessions: dict[str, IssuedSession] = {}
        self.password = SecretStr("placeholder" + tenant)

    async def login(self, username: str, password: SecretStr) -> IssuedSession:
        if (
            username != "member"
            or password.get_secret_value() != self.password.get_secret_value()
        ):
            raise AuthenticationDenied()
        issued = IssuedSession(
            principal=self.principal,
            token=SecretStr(secrets.token_urlsafe(32)),
            csrf_token=SecretStr(secrets.token_urlsafe(32)),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.sessions[issued.token.get_secret_value()] = issued
        return issued

    async def get_session(self, token: SecretStr) -> IssuedSession:
        issued = self.sessions.get(token.get_secret_value())
        if issued is None:
            raise AuthenticationDenied()
        return issued

    async def authenticate(
        self, token: SecretStr, *, csrf_token: SecretStr | None = None
    ) -> AuthPrincipal:
        issued = await self.get_session(token)
        if (
            csrf_token is not None
            and csrf_token.get_secret_value() != issued.csrf_token.get_secret_value()
        ):
            raise AuthenticationDenied()
        return issued.principal

    async def logout(self, token: SecretStr) -> None:
        self.sessions.pop(token.get_secret_value(), None)


def child(
    key: str,
    *,
    events: list[str] | None = None,
    fail_start: bool = False,
    explicit_cookie: bool = True,
):
    from apps.api.authentication import AuthenticationCookieSettings

    tenant = TenantId("tenant-" + key)
    authentication = MemoryAuthentication(tenant)
    employee = EmployeeView(
        employee_id=EmployeeId("same-employee"),
        tenant_id=tenant,
        user_id=UserId("same-user"),
        name="合成员工",
        role="sales",
    )

    class Employees:
        async def get_employee(self, scope_tenant, employee_id, *, actor):
            assert scope_tenant == tenant and employee_id == employee.employee_id
            return employee

    @asynccontextmanager
    async def employees(scope_tenant):
        assert scope_tenant == tenant
        yield Employees()

    engine = create_async_engine("postgresql+asyncpg://unused/unused")

    @asynccontextmanager
    async def lifespan(app):
        if events is not None:
            events.append("enter:" + key)
        try:
            if fail_start:
                raise RuntimeError("synthetic startup failure")
            yield
        finally:
            if events is not None:
                events.append("exit:" + key)
            await engine.dispose()

    dependencies = replace(_app()[0].state.dependencies, employees=employees)
    kwargs = (
        {
            "authentication_cookie": AuthenticationCookieSettings(
                "tradeos_session_" + key, "/api/enterprises/" + key
            )
        }
        if explicit_cookie
        else {}
    )
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=False, retry_after_seconds=30),
        dependencies=dependencies,
        authentication=authentication,
        authentication_origin=ORIGIN,
        lifespan=lifespan,
        **kwargs,
    )
    app.state.runtime_engine = engine
    records = {"shared-id": key + "-private", key + "-only": key + "-exclusive"}

    @app.get("/records/{record_id}")
    async def read_record(
        record_id: str,
        identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    ):
        if record_id not in records:
            raise HTTPException(404)
        await asyncio.sleep(0)
        return {"tenant": identity.tenant_id, "value": records[record_id]}

    @app.post("/records/{record_id}")
    async def update_record(
        record_id: str,
        identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    ):
        if record_id not in records:
            raise HTTPException(404)
        records[record_id] = key + "-updated"
        return {"tenant": identity.tenant_id, "value": records[record_id]}

    return app, authentication


async def no_database_probe(engine: AsyncEngine, tenant_id: str) -> None:
    """显式替换数据库探针；本文件不声称验证 RLS。"""
    assert isinstance(engine, AsyncEngine) and tenant_id.startswith("tenant-")


def container(a=None, b=None, *, probe=no_database_probe):
    from apps.api.enterprise_container import (
        EnterpriseAppBinding,
        create_enterprise_container,
    )

    a = a or child("a")[0]
    b = b or child("b")[0]
    return create_enterprise_container(
        (
            EnterpriseAppBinding("a", TenantId("tenant-a"), a),
            EnterpriseAppBinding("b", TenantId("tenant-b"), b),
        ),
        isolation_probe=probe,
    )


async def login(client, key, auth, *, prefix=None):
    result = await client.post(
        (prefix or "/api/enterprises/" + key) + "/auth/login",
        headers=TRUSTED,
        json={"username": "member", "password": auth.password.get_secret_value()},
    )
    assert result.status_code == 200, "synthetic_login_failed"
    return result


async def test_default_single_enterprise_cookie_and_logout_remain_compatible():
    business, auth = child("a", explicit_cookie=False)
    root = FastAPI()
    root.mount("/api", business)
    async with (
        business.router.lifespan_context(business),
        AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client,
    ):
        result = await login(client, "a", auth, prefix="/api")
        value = result.headers["set-cookie"]
        assert value.startswith("tradeos_session_18761=") and "Path=/api;" in value
        assert (await client.get("/api/auth/session")).status_code == 200
        assert (
            await client.post(
                "/api/auth/logout",
                headers={**TRUSTED, "X-CSRF-Token": result.json()["csrf_token"]},
            )
        ).status_code == 204
        assert (await client.get("/api/auth/session")).status_code == 401


async def test_two_enterprises_keep_cookie_and_same_id_data_separate_under_concurrency():
    a, aa = child("a")
    b, ab = child("b")
    root = container(a, b)
    async with (
        root.router.lifespan_context(root),
        AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client,
    ):
        la = await login(client, "a", aa)
        lb = await login(client, "b", ab)
        assert "Path=/api/enterprises/a;" in la.headers["set-cookie"]
        assert "Path=/api/enterprises/b;" in lb.headers["set-cookie"]
        replies = await asyncio.gather(
            *(
                client.get("/api/enterprises/" + key + "/records/shared-id")
                for key in ["a", "b"] * 12
            )
        )
        assert [r.json()["value"] for r in replies] == ["a-private", "b-private"] * 12
        changed = await client.post(
            "/api/enterprises/a/records/shared-id",
            headers={**TRUSTED, "X-CSRF-Token": la.json()["csrf_token"]},
        )
        assert changed.status_code == 200
        assert (await client.get("/api/enterprises/b/records/shared-id")).json()[
            "value"
        ] == "b-private"
        assert (
            await client.get("/api/enterprises/a/records/b-only")
        ).status_code == 404
        await client.post(
            "/api/enterprises/a/auth/logout",
            headers={**TRUSTED, "X-CSRF-Token": la.json()["csrf_token"]},
        )
        assert (await client.get("/api/enterprises/a/auth/session")).status_code == 401
        assert (await client.get("/api/enterprises/b/auth/session")).status_code == 200


async def test_cross_enterprise_cookie_csrf_and_identity_headers_cannot_authorize():
    a, aa = child("a")
    b, ab = child("b")
    root = container(a, b)
    async with (
        root.router.lifespan_context(root),
        AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client,
    ):
        la = await login(client, "a", aa)
        await login(client, "b", ab)
        token_a = client.cookies.get("tradeos_session_a")
        for cookie in ("tradeos_session_a=" + token_a, "tradeos_session_b=" + token_a):
            result = await client.get(
                "/api/enterprises/b/records/shared-id", headers={"Cookie": cookie}
            )
            assert result.status_code == 401
        wrong_csrf = await client.post(
            "/api/enterprises/b/records/shared-id",
            headers={**TRUSTED, "X-CSRF-Token": la.json()["csrf_token"]},
        )
        assert wrong_csrf.status_code in {401, 403}
        forged = await client.get(
            "/api/enterprises/a/records/shared-id", headers={"X-Tenant-Id": "tenant-b"}
        )
        assert forged.status_code == 403
        assert (await client.get("/api/enterprises/b/records/shared-id")).json()[
            "value"
        ] == "b-private"


async def test_authenticated_wrong_tenant_principal_is_still_rejected():
    a, aa = child("a")
    root = container(a)
    aa.principal = aa.principal.model_copy(update={"tenant_id": TenantId("tenant-b")})
    async with (
        root.router.lifespan_context(root),
        AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client,
    ):
        response = await client.post(
            "/api/enterprises/a/auth/login",
            headers=TRUSTED,
            json={"username": "member", "password": aa.password.get_secret_value()},
        )
        assert response.status_code == 401
        assert not aa.sessions


async def test_unstarted_or_stopped_container_never_serves_registered_or_unknown_routes():
    root = container()
    async with AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client:
        assert (await client.get("/api/enterprises/a/auth/session")).status_code == 503
        async with root.router.lifespan_context(root):
            assert (
                await client.get("/api/enterprises/unknown/auth/session")
            ).status_code == 404
            assert (await client.get("/api/auth/session")).status_code == 404
            assert (
                await client.get("/api/enterprises/a/auth/session")
            ).status_code == 401
        assert (await client.get("/api/enterprises/a/auth/session")).status_code == 503


async def test_child_start_failure_closes_previous_child_and_never_sets_ready():
    from apps.api.enterprise_container import EnterpriseContainerStartupError

    events = []
    a, _ = child("a", events=events)
    b, _ = child("b", events=events, fail_start=True)
    root = container(a, b)
    with pytest.raises(EnterpriseContainerStartupError):
        async with root.router.lifespan_context(root):
            pytest.fail("failed startup became ready")
    assert events == ["enter:a", "enter:b", "exit:b", "exit:a"]
    async with AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client:
        assert (await client.get("/api/enterprises/a/auth/session")).status_code == 503


async def test_isolation_probe_failure_occurs_before_any_child_starts_and_disposes_engines(
    monkeypatch,
):
    from apps.api.enterprise_container import EnterpriseContainerStartupError

    events = []
    a, _ = child("a", events=events)
    b, _ = child("b", events=events)
    disposed = set()
    original = AsyncEngine.dispose

    async def dispose(engine, close=True):
        disposed.add(id(engine))
        await original(engine, close=close)

    async def deny_second(engine, tenant_id):
        if tenant_id == "tenant-b":
            raise ValueError("synthetic isolation failure")

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    root = container(a, b, probe=deny_second)
    with pytest.raises(EnterpriseContainerStartupError):
        async with root.router.lifespan_context(root):
            pytest.fail("unverified database became ready")
    assert events == []
    assert {id(a.state.runtime_engine), id(b.state.runtime_engine)} <= disposed


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_key",
        "duplicate_tenant",
        "shared_app",
        "shared_auth",
        "shared_engine",
        "shared_dependencies",
        "unauthenticated",
        "missing_engine",
        "wrong_tenant",
        "default_cookie",
        "cookie_mismatch",
        "invalid_key",
    ],
)
def test_untrusted_or_shared_binding_is_rejected_before_routing(fault):
    from apps.api.authentication import AuthenticationCookieSettings
    from apps.api.enterprise_container import (
        EnterpriseAppBinding,
        EnterpriseContainerConfigurationError,
        create_enterprise_container,
    )

    a, _ = child("a")
    b, _ = child("b", explicit_cookie=fault != "default_cookie")
    key, tenant = "b", TenantId("tenant-b")
    if fault == "duplicate_key":
        key = "a"
    elif fault == "duplicate_tenant":
        tenant = TenantId("tenant-a")
    elif fault == "shared_app":
        b = a
    elif fault == "shared_auth":
        b.state.authentication = a.state.authentication
    elif fault == "shared_engine":
        b.state.runtime_engine = a.state.runtime_engine
    elif fault == "shared_dependencies":
        b.state.dependencies = a.state.dependencies
    elif fault == "unauthenticated":
        b.state.authentication = None
    elif fault == "missing_engine":
        b.state.runtime_engine = None
    elif fault == "wrong_tenant":
        tenant = TenantId("tenant-other")
    elif fault == "cookie_mismatch":
        b.state.authentication_cookie = AuthenticationCookieSettings(
            "tradeos_session_b", "/api/enterprises/a"
        )
    elif fault == "invalid_key":
        key = "../a"
    with pytest.raises(EnterpriseContainerConfigurationError):
        create_enterprise_container(
            (
                EnterpriseAppBinding("a", TenantId("tenant-a"), a),
                EnterpriseAppBinding(key, tenant, b),
            ),
            isolation_probe=no_database_probe,
        )


@pytest.mark.parametrize(
    "name,path",
    [
        ("tradeos_session_x; injected=1", "/api"),
        ("tradeos_session_x", "/"),
        ("tradeos_session_x", "/api/../other"),
        ("tradeos_session_x", "/api; injected=1"),
        ("tradeos_session_x", "/api/"),
        ("__Host-x", "/api"),
    ],
)
def test_cookie_configuration_rejects_ambiguous_or_unbounded_scopes(name, path):
    from apps.api.authentication import AuthenticationCookieSettings

    with pytest.raises(ValueError):
        AuthenticationCookieSettings(name, path)
