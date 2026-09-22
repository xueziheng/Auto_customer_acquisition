"""配置版本和显式探测使用真实 PG；受控 Provider 不代表真实余额验证。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from domains.assistant.schemas import ModelSettingsUpdate
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.model_invocation import ModelGenerationError
from tests.integration.test_assistant import actor
from tests.integration.test_assistant_recovery import Provider, assemble, drain
from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.unit.test_standalone_model_settings import settings
from tool_gateway.fingerprint import HmacFingerprintProvider


class Administration:
    def __init__(self):
        self.allowed = True

    async def check(self, actor):
        from shared.errors import PermissionDenied

        if not self.allowed:
            raise PermissionDenied("已停用")

    async def require_admin(self, actor):
        await self.check(actor)


async def configured(engine, provider):
    from agent_runtime.assistant.model_authority import AssistantModelAuthority
    from apps.composition_support.model import build_model_composition
    from apps.scheduler_worker.assistant import AssistantDispatcher
    from domains.assistant.service_impl import ModelConfigurationServiceImpl
    from infra.db.model_configuration import SqlModelConfigurationRepository
    from infra.db.model_usage import SqlModelUsageRepository
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from workflows.assistant.flow import build_assistant_definition
    from workflows.assistant.steps import build_assistant_handlers

    app, _wf, _dispatcher, sessions, handlers, _identity = assemble(engine, provider)
    fp = HmacFingerprintProvider("test-v1", b"t" * 32)
    repo = SqlModelConfigurationRepository(sessions, fp)
    authority = Administration()
    service = ModelConfigurationServiceImpl(
        repo, authority, now=lambda: datetime.now(UTC)
    )
    config = StandaloneModelSettings.model_validate(settings())
    owner = actor()
    from infra.db.tables import EmployeeRow

    async with sessions.begin() as db:
        db.add(
            EmployeeRow(
                tenant_id=owner.tenant_id,
                employee_id=owner.employee_id,
                user_id=owner.user_id,
                name="测试管理员",
                role="boss",
                is_active=True,
            )
        )
    await repo.initialize(
        owner.tenant_id, config.configuration_version, config.model, config.limits, True
    )
    for process in ("api", "scheduler"):
        await repo.register_process(
            owner.tenant_id,
            process,
            config.configuration_version,
            f"instance-{process}",
        )

    class Resolver:
        def resolve(self, ref):
            pytest.fail("受控 Provider 不解析凭证")

    composition = build_model_composition(
        settings=config,
        resolver=Resolver(),
        authority=AssistantModelAuthority(service, app),
        usage=SqlModelUsageRepository(sessions),
        ledger_factory=lambda tenant: SqlAlchemyToolGatewayUnitOfWork(sessions, tenant),
        fingerprints=fp,
        lease_owner="probe-test",
        lease_duration=timedelta(seconds=30),
        provider_factory=lambda: provider,
    )
    ports = replace(
        handlers["assistant.generate"]._ports,
        model_generator=composition.generator,
        model=config.model,
        configuration_version=config.configuration_version,
        max_output_tokens=config.limits.max_output_tokens,
        configuration_service=service,
    )
    workflow = PostgresWorkflowEngine(sessions, build_assistant_handlers(ports))
    workflow.register(build_assistant_definition())
    return (
        owner,
        app,
        workflow,
        AssistantDispatcher(app, workflow),
        repo,
        service,
        config,
        authority,
    )


async def test_start_is_free_explicit_probe_is_once_and_hidden_from_chat(unit_engine):
    provider = Provider(text='{"ok":true}')
    owner, app, wf, dispatcher, _repo, service, config, _authority = await configured(
        unit_engine, provider
    )
    assert provider.calls == 0
    assert (await service.get_public(owner)).status == "unverified"
    with pytest.raises(ModelGenerationError):
        await service.authorize(owner, config.configuration_version, probe=False)
    first = await service.request_probe(owner, "probe-once")
    assert (await service.request_probe(owner, "probe-once")).turn_id == first.turn_id
    assert await app.list_sessions(owner) == []
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id)
    view = await service.get_public(owner)
    assert view.status == "verified" and view.verified_at is not None
    assert provider.calls == 1
    await service.authorize(owner, config.configuration_version, probe=False)
    assert (await service.request_probe(owner, "probe-once")).turn_id == first.turn_id
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id)
    assert provider.calls == 1


@pytest.mark.parametrize("action", ["rotate", "cancel", "revoke"])
async def test_late_probe_cannot_enable_after_configuration_or_authority_change(
    unit_engine, action
):
    provider = Provider(text='{"ok":true}')
    owner, app, wf, dispatcher, repo, service, config, authority = await configured(
        unit_engine, provider
    )
    turn = await service.request_probe(owner, "probe-once")

    async def changed():
        if action == "rotate":
            await service.save_nonsecret(
                owner,
                ModelSettingsUpdate(
                    expected_version=config.configuration_version,
                    model="new-model",
                    limits=config.limits,
                ),
            )
        elif action == "cancel":
            await app.cancel_turn(owner, turn.session_id, turn.turn_id)
        else:
            authority.allowed = False

    provider.after = changed
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id)
    snapshot = await repo.get(owner.tenant_id)
    assert snapshot.runtime.verified_at is None and provider.calls == 1
    if action == "rotate":
        assert service.view(snapshot).status == "pending_restart"
    else:
        assert (await app.execution(owner.tenant_id, turn.turn_id)).turn.state in {
            "blocked",
            "cancelled",
        }


async def test_configuration_and_runtime_migrations_roundtrip(unit_engine):
    assert migrate(unit_engine, "downgrade", "0063") == 0
    assert migrate(unit_engine, "upgrade", "head") == 0


async def test_recovery_keeps_unknown_cost_and_never_takes_current_owner(unit_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from infra.db.model_usage import SqlModelUsageRepository
    from shared.schemas.identifiers import TenantId
    from tests.integration.test_model_usage import identity, limits

    repo = SqlModelUsageRepository(async_sessionmaker(unit_engine))
    tenant = TenantId('tenant_recovery')
    now = datetime.now(UTC)
    claims = []
    for index, (owner, lease) in enumerate([
        ('old', now - timedelta(seconds=1)),
        ('old', now - timedelta(seconds=1)),
        ('current', now - timedelta(seconds=1)),
        ('old', now + timedelta(seconds=30)),
    ]):
        claim = await repo.reserve(identity(tenant), str(index) * 64,
            limits(tenant_calls=10, employee_calls=10, tenant_concurrency=10, employee_concurrency=10),
            now - timedelta(minutes=1), model='test-model', owner_id=owner,
            lease_expires_at=lease)
        claims.append(claim)
        if index != 0:
            await repo.mark_dispatched(tenant, claim.invocation_id)
    assert await repo.recover_abandoned(tenant, 'current', now) == 2
    views = [await repo.get(tenant, c.invocation_id) for c in claims]
    assert [v.state for v in views] == ['rejected', 'unknown', 'dispatched', 'dispatched']
    assert [v.slot_released for v in views] == [True, False, False, False]
    assert views[1].usage.input_tokens is None
    assert await repo.recover_abandoned(tenant, 'current', now) == 0


from tests.integration.test_pilot_persistence import initialized
from tests.integration.test_pilot_persistence import (
    owned_profiles as owned_profiles,  # noqa: PLC0414
)


def test_real_standalone_factories_authenticated_probe_and_chat(owned_profiles):
    import asyncio
    import secrets
    from pathlib import Path

    import httpx
    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from apps.api.standalone import create_standalone_app
    from apps.scheduler_worker.main import run_scheduler_worker
    from apps.scheduler_worker.standalone import create_standalone_factory
    from infra.authentication.service import PostgresAuthentication
    from infra.db.session import create_engine_from
    from infra.db.tables import EmployeeRow
    from shared.schemas.identifiers import EmployeeId, TenantId

    directory, profiles = owned_profiles
    profile = initialized(directory, profiles)
    config = StandaloneModelSettings.model_validate({**settings(), "limits": {**settings()["limits"], "tenant_calls": 10, "employee_calls": 10, "max_input_bytes": 65536}})
    provider = Provider(text='{"ok":true}')

    class Resolver:
        def resolve(self, ref):
            pytest.fail('受控测试不能解析模型凭证')

    async def exercise():
        engine = create_engine_from(profile.config.database_url.get_secret_value())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        tenant = TenantId(profile.config.tenant_id)
        auth = PostgresAuthentication(sessions, tenant)
        password = SecretStr(secrets.token_urlsafe(24))
        async with sessions.begin() as db:
            db.add(EmployeeRow(tenant_id=tenant, employee_id='emp_admin', user_id='usr_admin', name='测试老板', role='boss', is_active=True))
        await auth.create_account('synthetic', password, EmployeeId('emp_admin'))
        await engine.dispose()
        app = create_standalone_app(profile.config, config, Path('apps/web/dist').resolve())
        factory = create_standalone_factory(profile.config, config, Resolver(), provider_factory=lambda: provider)
        origin = f'http://127.0.0.1:{profile.config.api_port}'
        async with app.router.lifespan_context(app), factory() as runtime, httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=origin) as client:
            trusted = {'Origin': origin, 'X-TradeOS-Request': '1'}
            logged = await client.post('/api/auth/login', headers=trusted, json={'username':'synthetic', 'password':password.get_secret_value()})
            assert logged.status_code == 200, 'AUTH_LOGIN_FAILED'
            headers = {**trusted, 'X-CSRF-Token': logged.json()['csrf_token']}
            assert provider.calls == 0
            stop = asyncio.Event()
            stage = 0
            path = ''
            async def next_cycle(interval, event):
                nonlocal stage, path
                if stage == 0:
                    view = await client.get('/api/settings/model')
                    assert view.status_code == 200 and view.json()['status'] == 'unverified'
                    for _ in range(2):
                        response = await client.post('/api/settings/model/probe', headers=headers, json={'idempotency_key':'same-probe'})
                        assert response.status_code == 202
                elif stage == 8:
                    view = await client.get('/api/settings/model')
                    assert view.json()['status'] == 'verified', (view.json()['failure_code'], provider.calls)
                    assert provider.calls == 1
                    provider.text = '{"kind":"clarify","questions":["请说明研究国家。"],"missing_fields":["target_countries"]}'
                    created = await client.post('/api/agent/sessions', headers=headers, json={})
                    assert created.status_code == 201
                    path = f"/api/agent/sessions/{created.json()['session_id']}/turns"
                    accepted = await client.post(path, headers=headers, json={'text':'我想研究市场', 'idempotency_key':'chat-once'})
                    assert accepted.status_code == 202, accepted.text
                elif stage == 16:
                    history = await client.get(path)
                    assert history.status_code == 200
                    assert history.json()[0]['state'] == 'awaiting_input', history.json()
                    assert provider.calls == 2
                    stop.set()
                stage += 1
            await run_scheduler_worker(runtime, stop_event=stop, wait=next_cycle, install_signal_handlers=False)
    asyncio.run(exercise())
