"""真实认证和 PG 私有会话，经产品 HTTP 提交；HTTP 不调用模型。"""

from dataclasses import replace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from agent_runtime.assistant.context import HistoryProjector
from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from domains.assistant.service_impl import AssistantServiceImpl
from infra.db.assistant import SqlAssistantRepository
from infra.db.tables import EmployeeRow
from tests.integration.test_api_session_authentication import (
    ORIGIN,
    TRUSTED,
    login,
    setup_api,
)
from tests.integration.test_assistant import actor as new_actor
from tests.integration.test_assistant_recovery import Reads
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tool_gateway.fingerprint import HmacFingerprintProvider


async def configured(engine):
    app, auth, factory, tenant, employee, password = await setup_api(engine)
    api = next(r.app for r in app.routes if getattr(r, "path", None) == "/api")
    deps = api.state.dependencies
    identity = CurrentEmployeeIdentity(deps.employees, deps.employee_lookup_actor)
    repo = SqlAssistantRepository(factory)
    service = AssistantServiceImpl(
        repo,
        identity,
        CredentialMarkerGuard(),
        HmacFingerprintProvider("test-v1", b"p" * 32),
        HistoryProjector(identity, Reads(), repo),
    )
    api.state.dependencies = replace(deps, assistant=service)
    return app, auth, factory, tenant, employee, password, service, repo


async def test_authenticated_accept_replay_conflict_and_csrf(unit_engine):
    (
        app,
        _auth,
        factory,
        tenant,
        employee,
        password,
        _service,
        _repo,
    ) = await configured(unit_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (
            await client.post("/api/agent/sessions", headers=TRUSTED, json={})
        ).status_code == 401
        logged = await login(client, password)
        headers = {**TRUSTED, "X-CSRF-Token": logged.json()["csrf_token"]}
        assert (
            await client.post("/api/agent/sessions", headers=TRUSTED, json={})
        ).status_code == 403
        created = await client.post("/api/agent/sessions", headers=headers, json={})
        assert created.status_code == 201
        path = f"/api/agent/sessions/{created.json()['session_id']}/turns"
        body = {"text": "只研究美国铰链", "idempotency_key": "accepted-once"}
        first = await client.post(path, headers=headers, json=body)
        assert first.status_code == 202 and first.headers["cache-control"] == "no-store"
        # 客户端丢弃第一次响应后依旧用原幂等键恢复。
        again = await client.post(path, headers=headers, json=body)
        assert (
            again.json()["turn_id"] == first.json()["turn_id"]
            and again.json()["run_id"] == first.json()["run_id"]
        )
        conflict = await client.post(
            path, headers=headers, json={**body, "text": "换内容"}
        )
        assert conflict.status_code == 409
        forged = await client.post(path, headers=headers, json={**body, "role": "boss"})
        assert forged.status_code == 422 and "只研究" not in forged.text
        history = await client.get(path)
        assert (
            history.status_code == 200
            and history.headers["cache-control"] == "no-store"
        )
        assert len(history.json()) == 1
        for role, can_view_run in (("boss", True), ("sales", False)):
            async with factory.begin() as db:
                await db.execute(
                    update(EmployeeRow)
                    .where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee)
                    .values(role=role)
                )
            current = await client.get(path)
            assert current.status_code == 200
            assert current.json()[0]["can_view_run"] is can_view_run


async def test_other_private_session_hidden_and_disabled_user_denied(
    unit_engine,
):
    app, _auth, factory, tenant, employee, password, _service, repo = await configured(
        unit_engine
    )
    outsider = new_actor(tenant=tenant)
    session = await repo.create(outsider)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        await login(client, password)
        assert (
            await client.get(f"/api/agent/sessions/{session.session_id}/turns")
        ).status_code == 404
        async with factory.begin() as db:
            await db.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee
                )
                .values(is_active=False)
            )
        assert (await client.get("/api/agent/sessions")).status_code in {401, 403}


async def test_unconfigured_model_has_no_accept_path(unit_engine):
    app, _auth, _factory, _tenant, _employee, password = await setup_api(
        unit_engine
    )
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        logged = await login(client, password)
        response = await client.post(
            "/api/agent/sessions",
            headers={**TRUSTED, "X-CSRF-Token": logged.json()["csrf_token"]},
            json={},
        )
        assert response.status_code == 503 and "secret" not in response.text


async def test_cancel_response_also_applies_current_history_projection(
    unit_engine,
):
    (
        app,
        _auth,
        _factory,
        _tenant,
        _employee,
        password,
        service,
        _repo,
    ) = await configured(unit_engine)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        logged = await login(client, password)
        headers = {**TRUSTED, "X-CSRF-Token": logged.json()["csrf_token"]}
        created = await client.post("/api/agent/sessions", headers=headers, json={})
        path = f"/api/agent/sessions/{created.json()['session_id']}/turns"
        accepted = await client.post(
            path,
            headers=headers,
            json={"text": "PRIVATE_TEXT", "idempotency_key": "original"},
        )

        class RevokedProjector:
            async def project(self, actor, turn):
                return turn.model_copy(
                    update={"input_text": "", "content_hidden": True, "result": None}
                )

        service._projector = RevokedProjector()
        result = await client.post(
            f"{path}/{accepted.json()['turn_id']}/cancel", headers=headers, json={}
        )
        assert result.status_code == 200
        assert "PRIVATE_TEXT" not in result.text
