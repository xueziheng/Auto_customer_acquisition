"""Task 3a：真实 PostgreSQL 锁和 API 资源归属，不使用真实 Provider。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.runtime_config import Phase1RuntimeSettings
from apps.scheduler_worker.main import (
    SchedulerConfig,
    SchedulerRuntime,
    WorkerStartStatus,
    run_scheduler_worker,
)
from apps.scheduler_worker.runtime import SchedulerHealthState, _health_app
from connectors.object_store.config import S3ObjectStoreSettings
from infra.secrets import EnvironmentSecretResolver
from shared.schemas.identifiers import TenantId
from tests.integration.test_api_runtime import _runtime_env


def _health() -> SchedulerHealthState:
    state = SchedulerHealthState()
    for name in ("config", "schema", "database", "registry"):
        state.mark_ready(name)
    return state


class _Cycle:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def drain(self) -> int:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return 0

    async def poll_due(self, tenant_id: TenantId, limit: int) -> int:
        return 0


class _Activation:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def activate(self) -> None:
        self.entered.set()
        await self.release.wait()


def _runtime(
    engine: AsyncEngine,
    state: SchedulerHealthState,
    cycle: _Cycle,
    activation: _Activation | None = None,
) -> SchedulerRuntime:
    assert "lifecycle" in SchedulerRuntime.__dataclass_fields__, (
        "缺少 worker lifecycle observer"
    )
    return SchedulerRuntime(
        engine,
        cycle,
        cycle,
        TenantId("tenant-web-runtime"),
        SchedulerConfig(1, 2, 39053001),
        activation=activation,
        lifecycle=state,
    )


async def _ready(state: SchedulerHealthState) -> int:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_health_app(state)), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        return (await client.get("/health/ready")).status_code


async def _unlocked(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = 0 AND objid = 39053001 AND granted"
                )
            )
        ).scalar_one()
        assert count == 0, "原 backend 仍持锁，不能用同连接重入证明解锁"
        acquired = (
            await connection.execute(text("SELECT pg_try_advisory_lock(39053001)"))
        ).scalar_one()
        try:
            assert acquired is True
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(39053001)"))
            await connection.commit()


async def test_composed_scheduler_is_not_running_ready() -> None:
    assert await _ready(_health()) == 503


async def test_lock_activation_stopflag_and_second_singleton(
    integration_engine: AsyncEngine,
) -> None:
    state, cycle, activation = _health(), _Cycle(), _Activation()
    runtime = _runtime(integration_engine, state, cycle, activation)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_scheduler_worker(runtime, stop_event=stop, install_signal_handlers=False)
    )
    try:
        await asyncio.wait_for(activation.entered.wait(), 3)
        assert await _ready(state) == 503
        other_state, other_cycle = _health(), _Cycle()
        other = await run_scheduler_worker(
            _runtime(integration_engine, other_state, other_cycle),
            install_signal_handlers=False,
        )
        assert other.status is WorkerStartStatus.NOT_STARTED
        assert other.cycles_completed == 0
        assert other_cycle.calls == 0
        assert await _ready(other_state) == 503
        activation.release.set()
        await asyncio.wait_for(cycle.entered.wait(), 3)
        assert await _ready(state) == 200
        stop.set()
        assert await _ready(state) == 503
        assert not task.done(), "停止健康不得取消在途周期"
        cycle.release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.cycles_completed == 1
        assert await _ready(state) == 503
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await _unlocked(integration_engine)


@pytest.mark.parametrize("during_activation", [False, True])
async def test_cancellation_clears_health_and_unlocks(
    integration_engine: AsyncEngine, during_activation: bool
) -> None:
    state, cycle, activation = _health(), _Cycle(), _Activation()
    if not during_activation:
        activation.release.set()
    task = asyncio.create_task(
        run_scheduler_worker(
            _runtime(integration_engine, state, cycle, activation),
            install_signal_handlers=False,
        )
    )
    try:
        entered = activation.entered if during_activation else cycle.entered
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _ready(state) == 503
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await _unlocked(integration_engine)


class _Client:
    def __init__(self) -> None:
        self.calls = 0
        self.closes = 0

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        self.calls += 1
        return "{}"

    async def aclose(self) -> None:
        self.closes += 1


def _api(db_url: str, monkeypatch: pytest.MonkeyPatch):
    module = importlib.import_module("apps.api.runtime")
    builder = getattr(module, "create_runtime_app_from_settings", None)
    assert callable(builder), "缺少 typed API factory"
    env = _runtime_env(db_url)
    client = _Client()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("显式模型注入不得构造真实 Provider")

    monkeypatch.setattr("apps.api.composition.runtime.OpenAIJsonModelClient", forbidden)
    app = builder(
        Phase1RuntimeSettings.from_environ(env),
        secret_resolver=EnvironmentSecretResolver(env),
        object_store_settings=S3ObjectStoreSettings.from_environ(env),
        model_client=client,
    )
    return module, app, client


async def test_typed_factory_keeps_injected_model_borrowed(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, app, client = _api(db_url, monkeypatch)
    async with app.router.lifespan_context(app):
        assert await app.state.readiness_probe.is_ready()
        from shared.errors import ValidationError

        with pytest.raises(ValidationError, match="输出不满足"):
            await app.state.dependencies.trade_manager.propose_discovery(
                "Find demand for hinges"
            )
    assert client.calls == 1
    assert client.closes == 0
    assert app.state.runtime_engine.pool.checkedout() == 0


@pytest.mark.parametrize(
    "primary", [None, RuntimeError("primary-marker"), asyncio.CancelledError()]
)
async def test_api_cleanup_failure_attempts_every_resource_and_preserves_primary(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    primary: BaseException | None,
) -> None:
    module, app, _ = _api(db_url, monkeypatch)
    dependencies = app.state.dependencies
    order: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            order.append(self.name)
            raise RuntimeError("cleanup-private-marker")

    dependencies = replace(
        dependencies,
        model_lifecycle=Resource("model"),
        object_store_lifecycle=Resource("object"),
    )
    # 工厂闭包与真实 app 使用同一已构造依赖；只注入故障资源，不替换 DB/schema。
    original = module.build_phase1_dependencies
    monkeypatch.setattr(
        module, "build_phase1_dependencies", lambda *args, **kwargs: dependencies
    )
    env = _runtime_env(db_url)
    app = module.create_runtime_app_from_settings(
        Phase1RuntimeSettings.from_environ(env),
        secret_resolver=EnvironmentSecretResolver(env),
        object_store_settings=S3ObjectStoreSettings.from_environ(env),
        model_client=_Client(),
    )
    monkeypatch.setattr(module, "build_phase1_dependencies", original)
    original_dispose = AsyncEngine.dispose

    async def dispose(engine: AsyncEngine) -> None:
        order.append("engine")
        await original_dispose(engine)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    with pytest.raises(BaseException) as caught:
        async with app.router.lifespan_context(app):
            assert await app.state.readiness_probe.is_ready()
            if primary is not None:
                raise primary
    if primary is not None:
        assert caught.value is primary
    else:
        assert type(caught.value).__name__ == "RuntimeCleanupError"
        assert "cleanup-private-marker" not in str(caught.value)
    assert order == ["model", "object", "engine"]
    assert app.state.runtime_engine.pool.checkedout() == 0


@pytest.mark.parametrize("phase", ["starting", "running", "stopped"])
async def test_observer_failure_is_safe_and_cannot_block_unlock(
    integration_engine: AsyncEngine,
    phase: str,
) -> None:
    from apps.scheduler_worker.main import SchedulerLifecycleError

    state, cycle, stop = _health(), _Cycle(), asyncio.Event()

    class Observer:
        def starting(self, event: asyncio.Event) -> None:
            state.starting(event)
            if phase == "starting":
                raise RuntimeError("observer-private-marker")

        def running(self) -> None:
            state.running()
            if phase == "running":
                raise RuntimeError("observer-private-marker")
            stop.set()

        def stopped(self) -> None:
            state.stopped()
            if phase == "stopped":
                raise RuntimeError("observer-private-marker")

    cycle.release.set()
    runtime = replace(_runtime(integration_engine, state, cycle), lifecycle=Observer())
    with pytest.raises(SchedulerLifecycleError) as caught:
        await run_scheduler_worker(
            runtime, stop_event=stop, install_signal_handlers=False
        )
    assert "observer-private-marker" not in str(caught.value)
    assert await _ready(state) == 503
    await _unlocked(integration_engine)


@pytest.mark.parametrize("during_activation", [False, True])
async def test_real_backend_loss_revokes_health_and_prevents_following_cycle(
    integration_engine: AsyncEngine,
    during_activation: bool,
) -> None:
    from tests.integration.test_scheduler_worker import _lock_holder

    state, cycle = _health(), _Cycle()
    cycle.release.set()

    async def terminate() -> None:
        pid, _ = await _lock_holder(integration_engine, 39053001)
        async with integration_engine.begin() as connection:
            assert (
                await connection.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                )
            ).scalar_one()

    class Activation:
        async def activate(self) -> None:
            assert await _ready(state) == 503
            await terminate()

    async def wait(interval: float, stop_event: asyncio.Event) -> None:
        assert await _ready(state) == 200
        await terminate()

    runtime = _runtime(integration_engine, state, cycle)
    if during_activation:
        runtime = replace(runtime, activation=Activation())
    result = await run_scheduler_worker(
        runtime, wait=wait, install_signal_handlers=False
    )
    assert result.status is WorkerStartStatus.LOCK_LOST
    assert result.cycles_completed == (0 if during_activation else 1)
    assert cycle.calls == (0 if during_activation else 1)
    assert await _ready(state) == 503
    await _unlocked(integration_engine)


async def test_activation_failure_is_primary_even_when_observer_cleanup_fails(
    integration_engine: AsyncEngine,
) -> None:
    state, cycle = _health(), _Cycle()
    primary = RuntimeError("activation-marker")

    class Activation:
        async def activate(self) -> None:
            raise primary

    class Observer(SchedulerHealthState):
        def stopped(self) -> None:
            super().stopped()
            raise RuntimeError("observer-marker")

    observer = Observer()
    runtime = replace(
        _runtime(integration_engine, state, cycle),
        activation=Activation(),
        lifecycle=observer,
    )
    with pytest.raises(RuntimeError) as caught:
        await run_scheduler_worker(runtime, install_signal_handlers=False)
    assert caught.value is primary
    assert not observer.is_ready
    assert cycle.calls == 0
    await _unlocked(integration_engine)


async def test_default_api_owns_opened_model_and_object_clients(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from connectors.openai import OpenAIJsonModelClient

    module = importlib.import_module("apps.api.runtime")
    env = _runtime_env(db_url)
    env["OPENAI_API_KEY"] = "m" * 32
    env["OPENAI_API_KEY_REF"] = "OPENAI_API_KEY"
    closed: list[str] = []

    class ModelSDK:
        async def close(self) -> None:
            closed.append("model")

    class ObjectSDK:
        def put_object(self, **kwargs: object) -> None:
            pass

        def close(self) -> None:
            closed.append("object")

    monkeypatch.setattr(
        OpenAIJsonModelClient, "_default_client", staticmethod(lambda *args: ModelSDK())
    )
    monkeypatch.setattr(
        "connectors.object_store.s3.boto3.client", lambda *args, **kwargs: ObjectSDK()
    )
    app = module.create_runtime_app_from_settings(
        Phase1RuntimeSettings.from_environ(env),
        secret_resolver=EnvironmentSecretResolver(env),
        object_store_settings=S3ObjectStoreSettings.from_environ(env),
    )
    async with app.router.lifespan_context(app):
        resources = app.state.dependencies
        # 直接执行资源端口以隔离业务授权；没有外部网络或业务写入。
        await resources.model_lifecycle._get_client()
        await resources.object_store_lifecycle.put(
            "raw/tn_01K00000000000000000000001/art_01K00000000000000000000002", b"owned"
        )
        assert closed == []
        assert await app.state.readiness_probe.is_ready()
    assert closed == ["model", "object"]
    assert app.state.runtime_engine.pool.checkedout() == 0


async def test_loopback_health_server_serves_composed_not_ready() -> None:
    import socket

    from apps.scheduler_worker.runtime import SchedulerHealthServer

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = SchedulerHealthServer(_health(), port, host="127.0.0.1")
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(server.wait_started(), 3)
        assert all(
            sock.getsockname()[0] == "127.0.0.1"
            for item in server._server.servers
            for sock in item.sockets
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as client:
            assert (await client.get("/health/ready")).status_code == 503
    finally:
        await server.close()
        await asyncio.wait_for(task, 3)


@pytest.mark.parametrize("checkpoint", ["schema", "registry"])
async def test_missing_checkpoint_cannot_become_running_ready(
    integration_engine: AsyncEngine,
    checkpoint: str,
) -> None:
    state, cycle, stop = SchedulerHealthState(), _Cycle(), asyncio.Event()
    for item in ("config", "schema", "database", "registry"):
        if item != checkpoint:
            state.mark_ready(item)
    task = asyncio.create_task(
        run_scheduler_worker(
            _runtime(integration_engine, state, cycle),
            stop_event=stop,
            install_signal_handlers=False,
        )
    )
    try:
        await asyncio.wait_for(cycle.entered.wait(), 3)
        assert await _ready(state) == 503
        stop.set()
        cycle.release.set()
        await asyncio.wait_for(task, 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "failure", [RuntimeError("dispose-private-marker"), asyncio.CancelledError()]
)
async def test_api_engine_cleanup_failure_is_visible(
    db_url: str, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    module, app, _ = _api(db_url, monkeypatch)
    dispose = AsyncEngine.dispose

    async def failing_dispose(engine: AsyncEngine) -> None:
        await dispose(engine)
        raise failure

    monkeypatch.setattr(AsyncEngine, "dispose", failing_dispose)
    with pytest.raises(BaseException) as caught:
        async with app.router.lifespan_context(app):
            assert await app.state.readiness_probe.is_ready()
    if isinstance(failure, Exception):
        assert isinstance(caught.value, module.RuntimeCleanupError)
        assert "dispose-private-marker" not in str(caught.value)
    else:
        assert caught.value is failure
    assert app.state.runtime_engine.pool.checkedout() == 0


@pytest.mark.parametrize(
    "failure", [RuntimeError("startup-private-marker"), asyncio.CancelledError()]
)
async def test_actual_api_quotation_start_failure_closes_parser_and_database(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    import json

    from connectors.evidence_text.client import LinuxEvidenceTextParser
    from tests.quotation_runtime_fixtures import quotation_settings_values

    module = importlib.import_module("apps.api.runtime")
    env = _runtime_env(db_url)
    env["TRADEOS_QUOTATION_SETTINGS_JSON"] = json.dumps(quotation_settings_values())
    closed: list[str] = []

    async def probe(parser: LinuxEvidenceTextParser) -> None:
        raise failure

    parser_close = LinuxEvidenceTextParser.aclose

    async def close(parser: LinuxEvidenceTextParser) -> None:
        await parser_close(parser)
        closed.append("parser")

    monkeypatch.setattr(LinuxEvidenceTextParser, "probe", probe)
    monkeypatch.setattr(LinuxEvidenceTextParser, "aclose", close)
    app = module.create_runtime_app_from_settings(
        Phase1RuntimeSettings.from_environ(env),
        secret_resolver=EnvironmentSecretResolver(env),
        object_store_settings=S3ObjectStoreSettings.from_environ(env),
        model_client=_Client(),
    )
    with pytest.raises(BaseException) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("失败 startup 不能 yield")
    if isinstance(failure, Exception):
        assert str(caught.value) == "报价运行依赖启动失败"
    else:
        assert caught.value is failure
    assert closed == ["parser"]
    assert app.state.runtime_engine.pool.checkedout() == 0


async def test_actual_scheduler_missing_step_registration_rejects_before_health(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.scheduler_worker import runtime as worker
    from tests.integration.test_scheduler_worker import (
        _factory_dependencies,
        _factory_environ,
        _FactoryResolver,
    )

    disposed: list[AsyncEngine] = []
    dispose = AsyncEngine.dispose

    async def close(engine: AsyncEngine) -> None:
        await dispose(engine)
        disposed.append(engine)

    def forbidden_health(*args: object) -> None:
        pytest.fail("缺步骤注册不能启动健康服务")

    monkeypatch.setattr(AsyncEngine, "dispose", close)
    monkeypatch.setattr(
        worker, "build_human_handoff_step_handlers", lambda *args, **kwargs: {}
    )
    factory = worker.SchedulerRuntimeFactory(
        _factory_environ(
            str(db_url), TenantId("tn_01K00000000000000000000003"), hunter_enabled=False
        ),
        _factory_dependencies(worker, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=forbidden_health,
    )
    with pytest.raises(ValueError, match="未注册的 handler_ref"):
        async with factory():
            pytest.fail("缺步骤不能 yield runtime")
    assert len(disposed) == 1
    assert disposed[0].pool.checkedout() == 0


@pytest.mark.parametrize("window", ["commit", "query_result"])
async def test_cancel_after_lock_acquired_before_first_commit_releases_session_lock(
    integration_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncConnection

    state, cycle = _health(), _Cycle()
    commit = AsyncConnection.commit
    first = True

    async def interrupted_commit(connection: AsyncConnection) -> None:
        nonlocal first
        if first:
            first = False
            raise asyncio.CancelledError()
        await commit(connection)

    execute = AsyncConnection.execute

    async def interrupted_execute(connection, statement, *args, **kwargs):
        result = await execute(connection, statement, *args, **kwargs)
        if "pg_try_advisory_lock" in str(statement):
            raise asyncio.CancelledError()
        return result

    if window == "commit":
        monkeypatch.setattr(AsyncConnection, "commit", interrupted_commit)
    else:
        monkeypatch.setattr(AsyncConnection, "execute", interrupted_execute)
    with pytest.raises(asyncio.CancelledError):
        await run_scheduler_worker(
            _runtime(integration_engine, state, cycle), install_signal_handlers=False
        )
    assert cycle.calls == 0
    assert await _ready(state) == 503
    monkeypatch.setattr(AsyncConnection, "execute", execute)
    await _unlocked(integration_engine)


async def test_unlock_cancellation_discards_original_connection_and_preserves_primary(
    integration_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncConnection

    state, cycle = _health(), _Cycle()
    primary = RuntimeError("activation-primary")

    class Activation:
        async def activate(self) -> None:
            raise primary

    execute = AsyncConnection.execute

    async def cancel_unlock(connection, statement, *args, **kwargs):
        if "pg_advisory_unlock" in str(statement):
            raise asyncio.CancelledError()
        return await execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", cancel_unlock)
    runtime = replace(
        _runtime(integration_engine, state, cycle), activation=Activation()
    )
    with pytest.raises(BaseException) as caught:
        await run_scheduler_worker(runtime, install_signal_handlers=False)
    assert caught.value is primary
    monkeypatch.setattr(AsyncConnection, "execute", execute)
    assert await _ready(state) == 503
    await _unlocked(integration_engine)
