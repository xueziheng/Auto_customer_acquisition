"""真实 PG 会话经同源 HTTP 的边界；敏感比较只输出布尔断言。"""

import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from apps.api.dependencies import get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import ApiSettings, create_app
from domains.employees.service_impl import EmployeeServiceImpl
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.tables import AuthSessionRow, EmployeeRow
from tests.integration.test_crm_api import _AllowAuthorizer, _Audit
from tests.integration.test_web_authentication import setup_auth
from tests.unit.test_crm_router import _app

ORIGIN = "http://127.0.0.1:18761"
TRUSTED = {"Origin": ORIGIN, "X-TradeOS-Request": "1"}


async def setup_api(engine, *, mounted=True, origin=ORIGIN):
    auth, factory, tenant, employee, password = await setup_auth(engine)

    @asynccontextmanager
    async def employees(scope_tenant):
        async with factory.begin() as session:
            repo = EmployeeRepositoryImpl(session, scope_tenant)
            yield EmployeeServiceImpl(
                employees=repo,
                territories=TerritoryRepositoryImpl(session, scope_tenant),
                ownership=OwnershipRepositoryImpl(session, scope_tenant),
                now=lambda: datetime.now(UTC),
                manager_pool=lambda _: (),
                count_active_accounts=repo.count_active_accounts,
                authorizer=_AllowAuthorizer(),
                audit=_Audit(),
            )

    dependencies = replace(_app()[0].state.dependencies, employees=employees)
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=False, retry_after_seconds=30),
        dependencies=dependencies,
        authentication=auth,
        authentication_origin=origin,
    )

    @app.get("/identity-test")
    async def identity_test(
        identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    ):
        return {
            "role": identity.employee.role,
            "owners": sorted(identity.opportunity_actor.scope.allowed_owners or []),
        }

    @app.post("/empty-command-test")
    async def empty_command_test():
        return {"accepted": True}

    @app.post("/raw-upload-test")
    async def raw_upload_test():
        return {"accepted": True}

    root = FastAPI()
    if mounted:
        root.mount("/api", app)
    return (root if mounted else app), auth, factory, tenant, employee, password


async def login(client, password):
    response = await client.post(
        "/api/auth/login",
        headers=TRUSTED,
        json={"username": "synthetic", "password": password.get_secret_value()},
    )
    assert response.status_code == 200, "AUTH_LOGIN_FAILED"
    return response


async def test_cookie_session_refresh_logout_mounted(integration_engine):
    app, _, _, _, employee, password = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await login(client, password)
        cookie_safe = all(
            part in response.headers.get("set-cookie", "").lower()
            for part in ("httponly", "samesite=strict", "path=/api")
        )
        token_absent = (
            "token" not in response.json() and "password" not in response.json()
        )
        assert cookie_safe, "AUTH_COOKIE_ATTRIBUTES_INVALID"
        assert token_absent, "AUTH_PRIVATE_RESPONSE_INVALID"
        session = await client.get("/api/auth/session")
        assert session.status_code == 200
        employee_matches = session.json()["employee"]["employee_id"] == employee
        assert employee_matches, "AUTH_EMPLOYEE_MISMATCH"
        stable = response.json()["csrf_token"] == session.json()["csrf_token"]
        assert stable, "AUTH_CSRF_CHANGED_ON_READ"
        private = session.headers.get("cache-control") == "no-store"
        assert private, "AUTH_RESPONSE_CACHEABLE"
        headers = {**TRUSTED, "X-CSRF-Token": session.json()["csrf_token"]}
        for path in ("empty-command-test", "raw-upload-test"):
            accepted = await client.post(
                f"/api/{path}",
                headers={**headers, "Content-Type": "image/png"},
                content=b"data" if path.startswith("raw") else b"",
            )
            assert accepted.status_code == 200
        result = await client.post("/api/auth/logout", headers=headers)
        assert result.status_code == 204
        assert (await client.get("/api/auth/session")).status_code == 401


@pytest.mark.parametrize(
    "fault",
    [
        "origin",
        "host",
        "custom",
        "csrf",
        "wrong_csrf",
        "fetch",
        "employee",
        "tenant",
        "duplicate_origin",
        "duplicate_host",
        "duplicate_csrf",
        "duplicate_cookie",
        "two_session_cookies",
        "duplicate_custom",
        "duplicate_fetch",
    ],
)
async def test_security_guards_reject_ambiguous_or_forged_requests(
    integration_engine, fault
):
    app, _, _, _, _, password = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        result = await login(client, password)
        headers = list({**TRUSTED, "X-CSRF-Token": result.json()["csrf_token"]}.items())
        if fault in {"origin", "custom", "csrf"}:
            name = {
                "origin": "Origin",
                "custom": "X-TradeOS-Request",
                "csrf": "X-CSRF-Token",
            }[fault]
            headers = [(k, v) for k, v in headers if k != name]
        elif fault == "wrong_csrf":
            headers = [
                (k, secrets.token_urlsafe(32) if k == "X-CSRF-Token" else v)
                for k, v in headers
            ]
        elif fault == "host":
            headers.append(("Host", "attacker.invalid"))
        elif fault == "fetch":
            headers.append(("Sec-Fetch-Site", "cross-site"))
        elif fault in {"employee", "tenant"}:
            headers.append((f"X-{fault.title()}-Id", "forged"))
        elif fault == "two_session_cookies":
            value = client.cookies.get("tradeos_session_18761")
            headers.append(
                (
                    "Cookie",
                    f"tradeos_session_18761={value}; tradeos_session_18761={value}",
                )
            )
        else:
            name = {
                "duplicate_origin": "Origin",
                "duplicate_host": "Host",
                "duplicate_csrf": "X-CSRF-Token",
                "duplicate_cookie": "Cookie",
                "duplicate_custom": "X-TradeOS-Request",
                "duplicate_fetch": "Sec-Fetch-Site",
            }[fault]
            value = dict(headers).get(
                name,
                ORIGIN.removeprefix("http://") if name == "Host" else "same-origin",
            )
            headers.extend([(name, value), (name, value)])
        response = await client.post("/api/auth/logout", headers=headers)
        assert response.status_code in {401, 403}, "AUTH_UNSAFE_REQUEST_ACCEPTED"


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"{}", "text/plain"),
        (b"{", "application/json"),
        (b"x" * 4097, "application/json"),
        (b'{"username":42,"password":[]}', "application/json"),
        (b'{"username":"UPPER","password":"invalid"}', "application/json"),
    ],
    ids=["mime", "json", "oversize", "field-types", "username"],
)
async def test_login_body_has_safe_bounded_validation(
    integration_engine, body, content_type
):
    app, *_ = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/auth/login",
            headers={**TRUSTED, "Content-Type": content_type},
            content=body,
        )
        assert response.status_code in {400, 413, 415}
        safe = set(response.json()) == {"code", "message"}
        assert safe, "AUTH_VALIDATION_REFLECTED_INPUT"
        private = response.headers.get("cache-control") == "no-store"
        assert private, "AUTH_RESPONSE_CACHEABLE"


async def test_current_employee_role_and_manager_scope_are_reloaded(integration_engine):
    app, _, factory, tenant, employee, password = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        await login(client, password)
        for role in ("boss", "manager", "sales"):
            async with factory.begin() as session:
                await session.execute(
                    update(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == tenant,
                        EmployeeRow.employee_id == employee,
                    )
                    .values(role=role)
                )
            response = await client.get("/api/identity-test")
            role_matches = response.json()["role"] == role
            assert role_matches, "AUTH_CURRENT_ROLE_STALE"
            assert (await client.get("/api/team/employees")).status_code == (
                200 if role == "boss" else 403
            )
        async with factory.begin() as session:
            await session.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee
                )
                .values(is_active=False)
            )
        assert (await client.get("/api/identity-test")).status_code == 401


@pytest.mark.parametrize("action", ["expire", "disable", "reset"])
async def test_expired_revoked_sessions_never_accept_development_headers(
    integration_engine, action
):
    app, auth, factory, tenant, _, password = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        await login(client, password)
        if action == "expire":
            async with factory.begin() as session:
                await session.execute(
                    update(AuthSessionRow)
                    .where(AuthSessionRow.tenant_id == tenant)
                    .values(
                        created_at=datetime.now(UTC) - timedelta(hours=9),
                        expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                )
        elif action == "disable":
            await auth.set_enabled("synthetic", False)
        else:
            await auth.reset_password("synthetic", password)
        assert (await client.get("/api/auth/session")).status_code == 401
        assert (
            await client.get(
                "/api/identity-test",
                headers={"X-Tenant-Id": tenant, "X-Employee-Id": "forged"},
            )
        ).status_code == 403


async def test_exact_anonymous_paths_and_host(integration_engine):
    app, *_ = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (await client.get("/api/unsubscribe/synthetic")).status_code == 200
        for path in ("/health/live", "/health/ready"):
            assert (
                await client.get("/api" + path)
            ).status_code == 404  # no probe injected
            assert (
                await client.get("/api" + path, headers={"Host": "wrong"})
            ).status_code == 403
        for path in (
            "/health/capabilities",
            "/health/other",
            "/health/live/",
            "/unsubscribe/a/b",
        ):
            assert (await client.get("/api" + path)).status_code == 401
        assert (
            await client.post("/api/health/live", headers=TRUSTED)
        ).status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://localhost:18761",
        "https://127.0.0.1:18761",
        "http://127.0.0.1",
        "http://127.0.0.1:18761/",
    ],
)
async def test_authentication_configuration_fails_closed(integration_engine, origin):
    auth, _, tenant, _, _ = await setup_auth(integration_engine)
    with pytest.raises(ValueError, match="authentication_configuration_invalid"):
        create_app(
            settings=ApiSettings(
                tenant_id=tenant, dev_mode=False, retry_after_seconds=30
            ),
            authentication=auth,
            authentication_origin=origin,
        )
    with pytest.raises(ValueError, match="authentication_configuration_invalid"):
        create_app(
            settings=ApiSettings(
                tenant_id=tenant, dev_mode=True, retry_after_seconds=30
            ),
            authentication=auth,
            authentication_origin=ORIGIN,
        )


async def test_login_rotation_revokes_old_cookie(integration_engine):
    app, *_, password = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        await login(client, password)
        old_cookie = client.cookies.get("tradeos_session_18761")
        await login(client, password)
        response = await client.get(
            "/api/auth/session",
            headers={"Cookie": f"tradeos_session_18761={old_cookie}"},
        )
        assert response.status_code == 401
        assert (await client.get("/api/auth/session")).status_code == 200


async def test_manager_membership_changes_on_next_request(integration_engine):
    from shared.schemas.identifiers import new_id

    app, _, factory, tenant, employee, password = await setup_api(integration_engine)
    subordinate = new_id("emp")
    async with factory.begin() as session:
        await session.execute(
            update(EmployeeRow)
            .where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee)
            .values(role="manager")
        )
        session.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=subordinate,
                user_id=new_id("usr"),
                name="下属",
                role="sales",
                is_active=True,
                manager_id=employee,
            )
        )
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        await login(client, password)
        owners_match = (await client.get("/api/identity-test")).json()[
            "owners"
        ] == sorted([employee, subordinate])
        assert owners_match, "AUTH_MANAGER_SCOPE_INVALID"
        async with factory.begin() as session:
            await session.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == tenant,
                    EmployeeRow.employee_id == subordinate,
                )
                .values(manager_id=None)
            )
        owners_match = (await client.get("/api/identity-test")).json()["owners"] == [
            employee
        ]
        assert owners_match, "AUTH_MANAGER_SCOPE_STALE"


async def test_login_schema_and_all_private_failure_no_store(integration_engine):
    app, *_ = await setup_api(integration_engine, mounted=False)
    schema = app.openapi()
    assert "LoginRequest" in schema["components"]["schemas"]
    assert "SessionResponse" in schema["components"]["schemas"]
    app.state.dependencies = None
    root = FastAPI()
    root.mount("/api", app)
    async with AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client:
        result = await client.get("/api/auth/session")
        private = result.headers.get("cache-control") == "no-store"
        assert private, "AUTH_RESPONSE_CACHEABLE"


async def test_login_errors_are_uniform_and_never_set_cookie(integration_engine):
    app, auth, _, _, _, password = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        wrong = await client.post(
            "/api/auth/login",
            headers=TRUSTED,
            json={"username": "synthetic", "password": secrets.token_urlsafe(24)},
        )
        missing = await client.post(
            "/api/auth/login",
            headers=TRUSTED,
            json={"username": "unknown", "password": password.get_secret_value()},
        )
        await auth.set_enabled("synthetic", False)
        disabled = await client.post(
            "/api/auth/login",
            headers=TRUSTED,
            json={"username": "synthetic", "password": password.get_secret_value()},
        )
        safe = all(
            r.status_code == 401
            and "set-cookie" not in r.headers
            and r.json() == wrong.json()
            for r in [wrong, missing, disabled]
        )
        assert safe, "AUTH_FAILURE_CONTRACT_CHANGED"


async def test_malformed_injected_principal_rejected(integration_engine):
    from shared.authentication import AuthPrincipal

    app, auth, _, tenant, employee, password = await setup_api(
        integration_engine, mounted=False
    )

    class MalformedService:
        async def authenticate(self, token, *, csrf_token=None):
            return AuthPrincipal.model_construct(
                tenant_id=tenant, employee_id=employee, user_id="wrong-user"
            )

    root = FastAPI()
    root.mount("/api", app)
    async with AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client:
        await login(client, password)
        auth.authenticate = MalformedService().authenticate
        assert (await client.get("/api/identity-test")).status_code == 401


async def test_login_stream_limit_and_duplicate_content_type(integration_engine):
    app, *_ = await setup_api(integration_engine)

    async def chunks():
        yield b" " * 4096
        yield b" "

    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/auth/login",
            headers={**TRUSTED, "Content-Type": "application/json"},
            content=chunks(),
        )
        assert response.status_code == 413
        response = await client.post(
            "/api/auth/login",
            headers=[
                *TRUSTED.items(),
                ("Content-Type", "application/json"),
                ("Content-Type", "application/json"),
            ],
            content=b"{}",
        )
        assert response.status_code == 403


async def test_raw_work_upload_preserves_existing_mime_and_body_limit(
    integration_engine,
):
    from tests.unit.test_work_uploads_router import _WorkUploads

    app, _, _, tenant, employee, password = await setup_api(
        integration_engine, mounted=False
    )
    uploads = _WorkUploads()
    uploads.maximum_upload_bytes = 8192
    app.state.dependencies = replace(app.state.dependencies, work_uploads=uploads)
    root = FastAPI()
    root.mount("/api", app)
    async with AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client:
        response = await login(client, password)
        headers = {
            **TRUSTED,
            "X-CSRF-Token": response.json()["csrf_token"],
            "Content-Type": "image/png",
        }
        result = await client.post(
            "/api/work-uploads?artifact_kind=image&source_kind=pdf_text&occurred_at=2026-08-22T12:00:00Z&customer_timezone=Asia%2FShanghai",
            headers=headers,
            content=b"x" * 5000,
        )
        assert result.status_code == 201
        assert uploads.upload_calls[0][:2] == (tenant, employee)
        assert uploads.upload_calls[0][-1]["mime_type"] == "image/png"
        assert len(uploads.upload_calls[0][-1]["content"]) == 5000


async def test_exact_anonymous_health_probes(integration_engine):
    from apps.api.routers.health import build_health_router

    app, *_ = await setup_api(integration_engine, mounted=False)

    class Ready:
        async def is_ready(self):
            return True

    app.include_router(build_health_router(Ready()))
    root = FastAPI()
    root.mount("/api", app)
    async with AsyncClient(transport=ASGITransport(root), base_url=ORIGIN) as client:
        for path, expected in [("live", "live"), ("ready", "ready")]:
            result = await client.get(f"/api/health/{path}")
            assert result.status_code == 200
            safe_payload = result.json() == {"status": expected}
            assert safe_payload, "AUTH_HEALTH_PAYLOAD_INVALID"
        assert (await client.get("/api/health/capabilities")).status_code == 401


async def test_logout_service_failure_never_claims_revocation(integration_engine):
    app, auth, _, _, _, password = await setup_api(integration_engine)

    async def unavailable(token):
        raise RuntimeError("controlled_failure")

    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await login(client, password)
        auth.logout = unavailable
        result = await client.post(
            "/api/auth/logout",
            headers={**TRUSTED, "X-CSRF-Token": response.json()["csrf_token"]},
        )
        assert result.status_code == 500
        unchanged = "set-cookie" not in result.headers
        private = result.headers.get("cache-control") == "no-store"
        assert unchanged, "AUTH_FAILED_LOGOUT_CHANGED_COOKIE"
        assert private, "AUTH_RESPONSE_CACHEABLE"
        assert (await client.get("/api/auth/session")).status_code == 200


async def test_login_api_rate_limit_mapping(integration_engine):
    app, *_ = await setup_api(integration_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        for _ in range(5):
            response = await client.post(
                "/api/auth/login",
                headers=TRUSTED,
                json={"username": "synthetic", "password": secrets.token_urlsafe(24)},
            )
            assert response.status_code == 401
        limited = await client.post(
            "/api/auth/login",
            headers=TRUSTED,
            json={"username": "synthetic", "password": secrets.token_urlsafe(24)},
        )
        assert limited.status_code == 429
        limited_code = limited.json()["code"] == "authentication_rate_limited"
        assert limited_code, "AUTH_RATE_LIMIT_CODE_INVALID"


async def test_two_loopback_ports_keep_independent_cookie_sessions(integration_engine):
    second_origin = "http://127.0.0.1:18762"
    first, _, _, _, first_employee, first_password = await setup_api(integration_engine)
    second, _, _, _, second_employee, second_password = await setup_api(
        integration_engine, origin=second_origin
    )

    async def dispatch(scope, receive, send):
        app = second if scope["server"][1] == 18762 else first
        await app(scope, receive, send)

    async with AsyncClient(
        transport=ASGITransport(dispatch), base_url=ORIGIN
    ) as client:
        first_login = await login(client, first_password)
        second_login = await client.post(
            second_origin + "/api/auth/login",
            headers={"Origin": second_origin, "X-TradeOS-Request": "1"},
            json={
                "username": "synthetic",
                "password": second_password.get_secret_value(),
            },
        )
        assert second_login.status_code == 200
        first_session = await client.get("/api/auth/session")
        assert first_session.status_code == 200
        first_matches = (
            first_session.json()["employee"]["employee_id"] == first_employee
        )
        assert first_matches, "AUTH_PORT_SESSION_MIXED"
        logout = await client.post(
            "/api/auth/logout",
            headers={**TRUSTED, "X-CSRF-Token": first_login.json()["csrf_token"]},
        )
        assert logout.status_code == 204
        other = await client.get(second_origin + "/api/auth/session")
        assert other.status_code == 200
        other_matches = other.json()["employee"]["employee_id"] == second_employee
        assert other_matches, "AUTH_OTHER_PORT_SESSION_CHANGED"
        assert (await client.get("/api/auth/session")).status_code == 401
