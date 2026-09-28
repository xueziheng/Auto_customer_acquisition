"""邮箱窄装配复用真实会话边界，不能顺带启用其他业务能力。"""

import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from apps.api.main import ApiSettings, create_app
from domains.conversations.mailbox import (
    MailboxService,
    MailboxView,
    MailThreadPage,
    require_mailbox_owner,
)
from domains.employees.permissions import Actor, EmployeeScope
from shared.authentication import AuthenticationDenied, AuthPrincipal, IssuedSession
from shared.schemas.identifiers import UserId
from tests.unit.test_api_app import _employee, _EmployeeService, _EmployeeServiceScope

ORIGIN = "http://127.0.0.1:18762"
TRUSTED = {"Origin": ORIGIN, "X-TradeOS-Request": "1"}


@pytest.fixture(autouse=True)
def restore_logging_disable() -> Iterator[None]:
    """CLI 的进程级日志保护不能污染同解释器内的后续测试。"""
    previous = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous)


class _Authentication:
    def __init__(self, employee):
        self.issued = IssuedSession(
            principal=AuthPrincipal(
                tenant_id=employee.tenant_id,
                employee_id=employee.employee_id,
                user_id=employee.user_id,
            ),
            token=SecretStr("synthetic-session"),
            csrf_token=SecretStr("synthetic-csrf"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.revoked = False

    async def login(self, username, password):
        return self.issued

    async def authenticate(self, token, *, csrf_token=None):
        if self.revoked or token != self.issued.token:
            raise AuthenticationDenied()
        if csrf_token is not None and csrf_token != self.issued.csrf_token:
            raise AuthenticationDenied()
        return self.issued.principal

    async def get_session(self, token):
        await self.authenticate(token)
        return self.issued

    async def logout(self, token):
        self.revoked = True


class _MailboxRepository:
    def __init__(self, employee):
        self.employee = employee
        self.requested = False

    def require(self, actor, mailbox_id="own-mailbox"):
        require_mailbox_owner(
            actor,
            self.employee.tenant_id,
            self.employee.employee_id,
            mailbox_id == "own-mailbox",
        )

    async def mailboxes(self, actor):
        self.require(actor)
        return [
            MailboxView(
                mailbox_id="own-mailbox",
                email="owner@example.invalid",
                phase="synced",
                message_count=25,
                last_synced_at=None,
                last_attempt_at=None,
                failure_code=None,
                sync_requested=self.requested,
            )
        ]

    async def threads(self, actor, mailbox_id, **kwargs):
        self.require(actor, mailbox_id)
        return MailThreadPage(items=[], next_offset=None)

    async def request_sync(self, actor, mailbox_id):
        self.require(actor, mailbox_id)
        self.requested = True


def _app():
    from apps.api.dependencies import MailboxApiDependencies

    employee = replace(_employee(role="viewer"), user_id=UserId("usr-owner"))
    employees = _EmployeeService(employee)
    repository = _MailboxRepository(employee)
    dependencies = MailboxApiDependencies(
        employees=_EmployeeServiceScope(employees),
        employee_lookup_actor=Actor(
            "system:mailbox-identity", EmployeeScope.SYSTEM, "system"
        ),
        mailbox=MailboxService(repository),
    )
    authentication = _Authentication(employee)
    api = create_app(
        settings=ApiSettings(
            tenant_id=employee.tenant_id, dev_mode=False, retry_after_seconds=30
        ),
        dependencies=dependencies,
        authentication=authentication,
        authentication_origin=ORIGIN,
    )
    root = FastAPI()
    root.mount("/api", api)
    return root, employees, authentication


async def _login(client):
    response = await client.post(
        "/api/auth/login",
        headers=TRUSTED,
        json={
            "username": "owner@example.invalid",
            "password": "synthetic unit-test password",
        },
    )
    assert response.status_code == 200
    return {**TRUSTED, "X-CSRF-Token": response.json()["csrf_token"]}


async def test_mailbox_only_login_read_sync_logout_and_business_disabled():
    app, _, _ = _app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (await client.get("/api/inbox/mailboxes")).status_code == 401
        headers = await _login(client)
        listed = await client.get("/api/inbox/mailboxes")
        assert listed.status_code == 200
        assert listed.json()[0]["message_count"] == 25
        assert listed.headers["Cache-Control"] == "no-store"
        assert (await client.get("/api/auth/session")).status_code == 200
        assert (
            await client.post("/api/inbox/mailboxes/own-mailbox/sync", headers=headers)
        ).status_code == 202
        assert (await client.get("/api/inbox/mailboxes")).json()[0][
            "sync_requested"
        ] is True
        assert (
            await client.get("/api/inbox/mailboxes/other-mailbox/threads")
        ).status_code == 403
        for path in (
            "/crm/opportunities",
            "/agent/sessions",
            "/settings/model",
            "/notifications",
        ):
            response = await client.get("/api" + path)
            assert response.status_code == 503, (path, response.status_code)
        capabilities = (await client.get("/api/health/capabilities")).json()
        assert capabilities and all(
            item["status"] == "disabled" for item in capabilities
        )
        assert (
            await client.post("/api/auth/logout", headers=headers)
        ).status_code == 204
        assert (await client.get("/api/inbox/mailboxes")).status_code == 401


@pytest.mark.parametrize("change", ["tenant", "employee", "user", "inactive"])
async def test_mailbox_identity_rechecks_current_employee_mapping(change):
    app, employees, _ = _app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        await _login(client)
        field, value = {
            "tenant": ("tenant_id", "another-tenant"),
            "employee": ("employee_id", "another-employee"),
            "user": ("user_id", "another-user"),
            "inactive": ("is_active", False),
        }[change]
        employees.employee = replace(employees.employee, **{field: value})
        assert (await client.get("/api/inbox/mailboxes")).status_code == 401


async def test_mailbox_session_rejects_development_identity_and_cross_origin():
    app, _, _ = _app()
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        await _login(client)
        for headers in (
            {"X-Employee-Id": "another-employee"},
            {"Origin": "http://attacker.invalid"},
        ):
            assert (
                await client.get("/api/inbox/mailboxes", headers=headers)
            ).status_code == 403


@pytest.mark.parametrize(
    "bad_actor",
    [
        Actor("", EmployeeScope.SYSTEM, "system"),
        Actor("system:mailbox", EmployeeScope.TENANT, "boss"),
    ],
)
def test_mailbox_dependencies_reject_broad_or_missing_lookup_actor(bad_actor):
    from apps.api.dependencies import MailboxApiDependencies

    with pytest.raises(ValueError):
        MailboxApiDependencies(
            employees=_EmployeeServiceScope(_EmployeeService(_employee())),
            employee_lookup_actor=bad_actor,
            mailbox=MailboxService(_MailboxRepository(_employee())),
        )


@pytest.mark.parametrize("failure", [None, "schema", "cancel", "cleanup"])
async def test_mailbox_runtime_checks_schema_and_disposes_even_on_failure(
    tmp_path, monkeypatch, failure
):
    from apps.api import mailbox

    events = []

    class Engine:
        async def dispose(self):
            events.append("dispose")
            if failure == "cleanup":
                raise RuntimeError("synthetic-private-material")

    async def schema(engine):
        events.append("schema")
        if failure == "schema":
            raise RuntimeError("synthetic-private-material")

    monkeypatch.setattr(mailbox, "create_engine_from", lambda _: Engine())
    monkeypatch.setattr(mailbox, "assert_database_schema_current", schema)
    (tmp_path / "index.html").write_text("<html>mailbox</html>")
    config = SimpleNamespace(
        tenant_id="tenant-mailbox",
        api_port=18762,
        origin=ORIGIN,
        database_url=SecretStr("postgresql+asyncpg://unused.invalid/mailbox"),
    )
    app = mailbox.create_mailbox_app(config, tmp_path)
    assert events == []
    if failure in {"schema", "cleanup"}:
        with pytest.raises(RuntimeError) as caught:
            async with app.router.lifespan_context(app):
                events.append("ready")
        assert "synthetic-private-material" not in str(caught.value)
    elif failure == "cancel":
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            async with app.router.lifespan_context(app):
                raise asyncio.CancelledError()
    else:
        async with app.router.lifespan_context(app):
            events.append("ready")
    assert events[0] == "schema"
    assert events[-1] == "dispose"
    assert ("ready" in events) is (failure in {None, "cleanup"})


@pytest.mark.parametrize("fault", ["origin", "build"])
def test_mailbox_runtime_rejects_non_loopback_or_missing_web_without_echo(
    tmp_path, fault
):
    from apps.api import mailbox

    if fault != "build":
        (tmp_path / "index.html").write_text("<html>mailbox</html>")
    config = SimpleNamespace(
        tenant_id="tenant-mailbox",
        api_port=18762,
        origin="http://synthetic-private-material.invalid:18762"
        if fault == "origin"
        else ORIGIN,
        database_url=SecretStr("postgresql+asyncpg://unused.invalid/mailbox"),
    )
    with pytest.raises(mailbox.RuntimeStartupError) as caught:
        mailbox.create_mailbox_app(config, tmp_path)
    assert "synthetic-private-material" not in str(caught.value)


def test_mailbox_cli_rejects_unknown_arguments_without_echo(monkeypatch, capsys):
    from apps.api import mailbox

    monkeypatch.setattr("sys.argv", ["mailbox", "--unknown=synthetic-private-material"])
    assert mailbox.main() == 2
    output = capsys.readouterr()
    assert "synthetic-private-material" not in output.out + output.err
