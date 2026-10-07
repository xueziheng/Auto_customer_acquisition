"""通知 worker 的配置、健康与循环生命周期。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from contextlib import suppress
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from notification_gateway.jobs import (
    NotificationContext,
    NotificationJobClaim,
    NotificationKind,
)
from notification_gateway.models import Notification, NotificationPriority
from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import EmployeeId, NotificationJobId, TenantId, new_id

_TENANT = TenantId(new_id("tn"))
_EMPLOYEE = EmployeeId(new_id("emp"))
_IDENTITY = new_id("sid")


def _module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：通知 worker 模块尚未实现（{exc}）")


def _config(**changes: object):
    Config = _module("apps.notification_worker.config").NotificationWorkerConfig
    values = {
        "database_url": SecretStr(
            "postgresql+asyncpg://" + "worker" + ":private@db/tradeos"
        ),
        "tenant_id": _TENANT,
        "poll_interval_seconds": 5,
        "batch_limit": 20,
        "health_port": 8093,
        "lease_owner": "notification-worker-1",
        "email": _email_settings(),
    }
    values.update(changes)
    return Config(**values)


def _environ() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://" + "worker" + ":private@db/tradeos",
        "TRADEOS_TENANT_ID": str(_TENANT),
        "TRADEOS_NOTIFICATION_POLL_INTERVAL_SECONDS": "5",
        "TRADEOS_NOTIFICATION_BATCH_LIMIT": "20",
        "TRADEOS_NOTIFICATION_HEALTH_PORT": "8093",
        "TRADEOS_NOTIFICATION_LEASE_OWNER": "notification-worker-1",
        "TRADEOS_NOTIFICATION_GMAIL_BASE_URL": "https://gmail.googleapis.com",
        "TRADEOS_NOTIFICATION_SENDING_IDENTITY_ID": _IDENTITY,
        "TRADEOS_NOTIFICATION_RECIPIENTS_JSON": json.dumps(
            [
                {
                    "tenant_id": str(_TENANT),
                    "employee_id": str(_EMPLOYEE),
                    "address": "owner@example.com",
                }
            ],
            separators=(",", ":"),
        ),
        "GMAIL_OAUTH_TOKEN_REF": "NOTIFICATION_GMAIL_VALUE",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "NOTIFICATION_FINGERPRINT_VALUE",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "notification-v1",
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TRADEOS_DEV_MODE": "false",
    }


def _email_settings():
    config_module = _module("apps.notification_worker.config")
    recipient_module = _module("apps.notification_worker.recipients")
    secrets_module = _module("infra.secrets")
    recipients = recipient_module.ConfiguredNotificationRecipientDirectory.from_value(
        [
            {
                "tenant_id": str(_TENANT),
                "employee_id": str(_EMPLOYEE),
                "address": "owner@example.com",
            }
        ]
    )
    return config_module.NotificationEmailSettings(
        "https://gmail.googleapis.com",
        _IDENTITY,
        recipients,
        "NOTIFICATION_GMAIL_VALUE",
        "NOTIFICATION_FINGERPRINT_VALUE",
        "notification-v1",
        120,
        secrets_module.EnvironmentSecretResolver(
            {
                "NOTIFICATION_GMAIL_VALUE": "o" * 32,
                "NOTIFICATION_FINGERPRINT_VALUE": "f" * 32,
            }
        ),
    )


def _patch_email_composition(
    monkeypatch: pytest.MonkeyPatch, module: object
) -> list[bool]:
    closed: list[bool] = []

    class Sender:
        async def send(self, _notification: Notification) -> None:
            return None

    class Transport:
        async def aclose(self) -> None:
            closed.append(True)

    async def compose(*_args, **_kwargs):
        return module.EmailNotificationChannel(Sender()), Transport()

    monkeypatch.setattr(module, "_compose_email_channel", compose)
    return closed


def test_config_requires_exact_explicit_values_and_safe_repr() -> None:
    """隐式 DSN/default 或 bool-as-int 会让生产 worker 错连环境。"""
    module = _module("apps.notification_worker.config")
    Config = module.NotificationWorkerConfig
    config = Config.from_environ(_environ())
    assert config == _config()
    assert "private" not in repr(config)
    for missing in _environ():
        environ = _environ()
        del environ[missing]
        with pytest.raises(module.NotificationWorkerConfigurationError):
            Config.from_environ(environ)
    for changes in (
        {"database_url": "raw"},
        {"tenant_id": TenantId("bad")},
        {"poll_interval_seconds": True},
        {"poll_interval_seconds": 0},
        {"batch_limit": 0},
        {"batch_limit": 101},
        {"health_port": 65536},
        {"lease_owner": "Bearer-private"},
    ):
        with pytest.raises(ValidationError):
            _config(**changes)
    bad = _environ()
    bad["TRADEOS_NOTIFICATION_BATCH_LIMIT"] = "020"
    with pytest.raises(module.NotificationWorkerConfigurationError):
        Config.from_environ(bad)


@pytest.mark.asyncio
async def test_health_has_only_exact_paths_no_redirects_and_fixed_bodies() -> None:
    """健康端点不能暴露 schema/config 细节或宽松重定向。"""
    module = _module("apps.notification_worker.health")
    state = module.NotificationHealthState()
    app = module.create_notification_health_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://health") as client:
        live = await client.get("/health/live")
        not_ready = await client.get("/health/ready")
        slash = await client.get("/health/live/", follow_redirects=False)
        missing = await client.get("/other")
        method = await client.post("/health/live")
        assert live.status_code == 200 and live.json() == {"status": "live"}
        assert not_ready.status_code == 503 and not_ready.json() == {
            "status": "not_ready"
        }
        assert slash.status_code == 404 and slash.json() == {
            "code": "not_found",
            "message": "资源不存在",
        }
        assert missing.json() == {"code": "not_found", "message": "资源不存在"}
        assert method.status_code == 405 and method.json() == {
            "code": "method_not_allowed",
            "message": "方法不允许",
        }
        for checkpoint in ("config", "schema", "database"):
            state.mark_ready(checkpoint)
        assert state.is_ready is False
        state.mark_ready("registry")
        assert state.is_ready is True
        ready = await client.get("/health/ready")
        assert ready.status_code == 200 and ready.json() == {"status": "ready"}
        state.mark_degraded()
        assert (await client.get("/health/ready")).status_code == 503
        state.mark_ok()
        assert (await client.get("/health/ready")).status_code == 200
    with pytest.raises(ValidationError):
        state.mark_ready("provider")


def _claim(index: int) -> NotificationJobClaim:
    return NotificationJobClaim(
        NotificationJobId(f"njb_{index}"),
        _TENANT,
        _EMPLOYEE,
        NotificationPriority.URGENT,
        NotificationContext(
            NotificationKind.COMMITMENT_OVERDUE,
            f"com_{index}",
            None,
            None,
            None,
        ),
        "CommitmentOverdue",
        f"dedup:{index}",
        f"claim:{index}",
        1,
    )


class _Jobs:
    def __init__(self, claims: tuple[NotificationJobClaim, ...]) -> None:
        self.claims = claims
        self.claim_calls: list[tuple[object, ...]] = []
        self.completed: list[tuple[object, ...]] = []
        self.retried: list[tuple[object, ...]] = []
        self.rejected: list[tuple[object, ...]] = []

    async def claim_due(self, tenant_id: TenantId, *, limit: int, lease_owner: str):
        self.claim_calls.append((tenant_id, limit, lease_owner))
        claims, self.claims = self.claims, ()
        return claims

    async def complete(self, tenant_id, job_id, *, claim_token):
        self.completed.append((tenant_id, job_id, claim_token))
        return True

    async def retry(self, tenant_id, job_id, *, claim_token, error):
        self.retried.append((tenant_id, job_id, claim_token, type(error).__name__))
        return True

    async def reject(self, tenant_id, job_id, *, claim_token, error):
        self.rejected.append((tenant_id, job_id, claim_token, type(error).__name__))
        return True


class _Renderer:
    def __init__(self) -> None:
        self.seen: list[NotificationJobClaim] = []

    def render(self, claim: NotificationJobClaim) -> Notification:
        self.seen.append(claim)
        return Notification(
            claim.tenant_id,
            claim.recipient,
            claim.priority,
            "固定标题",
            claim.context,
            claim.source_event,
            claim.dedup_key,
            source_job_id=claim.job_id,
        )


class _Router:
    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = outcomes
        self.seen: list[Notification] = []

    async def dispatch(self, notification: Notification) -> None:
        self.seen.append(notification)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


@pytest.mark.asyncio
async def test_worker_classifies_each_claim_independently_and_uses_fencing_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一个失败 claim 不能阻止后续成功；每次落状态必须带原 claim token。"""
    module = _module("apps.notification_worker.runtime")
    jobs = _Jobs((_claim(1), _claim(2), _claim(3), _claim(4)))
    router = _Router(
        [
            TransientError("Bearer payload must stay hidden"),
            PolicyViolation("pass" + "word=private"),
            RuntimeError("secret payload"),
            None,
        ]
    )
    health = _module("apps.notification_worker.health").NotificationHealthState()
    runtime = module.NotificationWorkerRuntime(jobs, router, _Renderer(), _config(), health)
    stop = asyncio.Event()

    async def wait(_seconds: int, _stop: asyncio.Event) -> None:
        stop.set()

    with caplog.at_level(logging.ERROR, logger="apps.notification_worker"):
        result = await module.run_notification_worker(
            runtime, stop_event=stop, wait=wait
        )
    assert result.status is module.WorkerRunStatus.STARTED
    assert (result.cycles_completed, result.jobs_completed) == (1, 1)
    assert jobs.claim_calls == [(_TENANT, 20, "notification-worker-1")]
    assert jobs.completed == [(_TENANT, NotificationJobId("njb_4"), "claim:4")]
    assert [item[1:] for item in jobs.retried] == [
        (NotificationJobId("njb_1"), "claim:1", "TransientError"),
        (NotificationJobId("njb_3"), "claim:3", "RuntimeError"),
    ]
    assert [item[1:] for item in jobs.rejected] == [
        (NotificationJobId("njb_2"), "claim:2", "PolicyViolation")
    ]
    rendered_logs = caplog.text.casefold()
    assert "bearer" not in rendered_logs
    assert "password" not in rendered_logs
    assert "secret payload" not in rendered_logs


@pytest.mark.asyncio
async def test_worker_rejects_cross_tenant_claim_before_renderer_or_router() -> None:
    """仓储返回 forged typed 跨租户 claim 时必须在渲染前失败关闭。"""
    module = _module("apps.notification_worker.runtime")
    foreign_claim = replace(_claim(1), tenant_id=TenantId(new_id("tn")))
    jobs = _Jobs((foreign_claim,))
    renderer = _Renderer()
    router = _Router([])
    health = _module("apps.notification_worker.health").NotificationHealthState()
    runtime = module.NotificationWorkerRuntime(jobs, router, renderer, _config(), health)
    stop = asyncio.Event()

    async def wait(_seconds: int, event: asyncio.Event) -> None:
        event.set()

    await module.run_notification_worker(runtime, stop_event=stop, wait=wait)

    assert renderer.seen == []
    assert router.seen == []
    assert jobs.retried == []
    assert [item[1:] for item in jobs.rejected] == [
        (foreign_claim.job_id, foreign_claim.claim_token, "TenantIsolationViolation")
    ]


class _FailingRenderer:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def render(self, _claim: NotificationJobClaim) -> Notification:
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_transition"),
    [
        (ValidationError("private validation"), "reject"),
        (PermissionDenied("private permission"), "reject"),
        (TransientError("private transient"), "retry"),
        (RuntimeError("private unexpected"), "retry"),
    ],
)
async def test_worker_classifies_renderer_poison_and_retryable_errors(
    error: BaseException, expected_transition: str
) -> None:
    """不可重试 TradeOSError 拒绝；暂态与显式 unexpected 策略才重试。"""
    module = _module("apps.notification_worker.runtime")
    jobs = _Jobs((_claim(1),))
    router = _Router([])
    health = _module("apps.notification_worker.health").NotificationHealthState()
    runtime = module.NotificationWorkerRuntime(
        jobs,
        router,
        _FailingRenderer(error),
        _config(),
        health,
    )
    stop = asyncio.Event()

    async def wait(_seconds: int, event: asyncio.Event) -> None:
        event.set()

    await module.run_notification_worker(runtime, stop_event=stop, wait=wait)

    assert router.seen == []
    if expected_transition == "reject":
        assert len(jobs.rejected) == 1 and jobs.retried == []
    else:
        assert len(jobs.retried) == 1 and jobs.rejected == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "source_event", "priority"),
    [
        (
            NotificationContext(
                NotificationKind.SENDING_IDENTITY_SUSPENDED,
                new_id("sid"),
                None,
                "customer_free_text",
                None,
            ),
            "SendingIdentitySuspended",
            NotificationPriority.URGENT,
        ),
        (
            NotificationContext(
                NotificationKind.REPUTATION_THRESHOLD_BREACHED,
                new_id("sid"),
                None,
                "customer_metric:customer_severity",
                None,
            ),
            "ReputationThresholdBreached",
            NotificationPriority.NORMAL,
        ),
    ],
)
async def test_worker_rejects_non_public_identity_reason_before_router(
    context: NotificationContext,
    source_event: str,
    priority: NotificationPriority,
) -> None:
    """合法 envelope 中的任意安全 reason 也必须在 router/dedup 前终止。"""
    module = _module("apps.notification_worker.runtime")
    renderer = _module(
        "notification_gateway.templates"
    ).FixedNotificationTemplateRenderer()
    claim = NotificationJobClaim(
        NotificationJobId(new_id("njb")),
        _TENANT,
        _EMPLOYEE,
        priority,
        context,
        source_event,
        "safe:dedup",
        new_id("njc"),
        1,
    )
    jobs = _Jobs((claim,))
    router = _Router([])
    health = _module("apps.notification_worker.health").NotificationHealthState()
    runtime = module.NotificationWorkerRuntime(jobs, router, renderer, _config(), health)
    stop = asyncio.Event()

    async def wait(_seconds: int, event: asyncio.Event) -> None:
        event.set()

    await module.run_notification_worker(runtime, stop_event=stop, wait=wait)

    assert router.seen == []
    assert jobs.retried == []
    assert [item[1:] for item in jobs.rejected] == [
        (claim.job_id, claim.claim_token, "ValidationError")
    ]


@pytest.mark.asyncio
async def test_worker_persistence_failure_does_not_stop_later_claims() -> None:
    """一条 claim 的结果落库失败也不能饿死同批后续任务。"""
    module = _module("apps.notification_worker.runtime")

    class Jobs(_Jobs):
        async def retry(self, tenant_id, job_id, *, claim_token, error):
            if job_id == NotificationJobId("njb_1"):
                raise RuntimeError("private persistence detail")
            return await super().retry(
                tenant_id,
                job_id,
                claim_token=claim_token,
                error=error,
            )

    jobs = Jobs((_claim(1), _claim(2)))
    health = _module("apps.notification_worker.health").NotificationHealthState()
    for checkpoint in ("config", "schema", "database", "registry"):
        health.mark_ready(checkpoint)
    assert health.is_ready is True
    runtime = module.NotificationWorkerRuntime(
        jobs,
        _Router([TransientError("private"), None]),
        _Renderer(),
        _config(),
        health,
    )
    stop = asyncio.Event()

    async def wait(_seconds: int, event: asyncio.Event) -> None:
        event.set()

    result = await module.run_notification_worker(
        runtime, stop_event=stop, wait=wait
    )
    assert result.jobs_completed == 1
    assert jobs.completed == [(_TENANT, NotificationJobId("njb_2"), "claim:2")]
    assert health.is_ready is False


@pytest.mark.asyncio
async def test_worker_stop_during_wait_and_signal_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop 只截断下一轮，且 signal handler 在正常退出时对称清理。"""
    module = _module("apps.notification_worker.runtime")
    jobs = _Jobs(())
    health = _module("apps.notification_worker.health").NotificationHealthState()
    runtime = module.NotificationWorkerRuntime(jobs, _Router([]), _Renderer(), _config(), health)
    stop = asyncio.Event()
    cleaned: list[bool] = []
    monkeypatch.setattr(module, "install_stop_signals", lambda _event: lambda: cleaned.append(True))

    async def wait(_seconds: int, event: asyncio.Event) -> None:
        event.set()

    result = await module.run_notification_worker(runtime, stop_event=stop, wait=wait)
    assert result.cycles_completed == 1
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_worker_cancellation_propagates_after_signal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError 不能被普通 claim 错误分支吞掉。"""
    module = _module("apps.notification_worker.runtime")
    jobs = _Jobs(())
    health = _module("apps.notification_worker.health").NotificationHealthState()
    runtime = module.NotificationWorkerRuntime(jobs, _Router([]), _Renderer(), _config(), health)
    entered = asyncio.Event()
    cleaned: list[bool] = []
    monkeypatch.setattr(module, "install_stop_signals", lambda _event: lambda: cleaned.append(True))

    async def wait(_seconds: int, _event: asyncio.Event) -> None:
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(module.run_notification_worker(runtime, wait=wait))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_worker_rejects_missing_email_config_before_claim() -> None:
    """direct config 不能绕过 production factory 后继续认领并终态化任务。"""
    module = _module("apps.notification_worker.runtime")
    jobs = _Jobs(())
    runtime = module.NotificationWorkerRuntime(
        jobs,
        _Router([]),
        _Renderer(),
        _config(email=None),
        _module("apps.notification_worker.health").NotificationHealthState(),
    )
    stop = asyncio.Event()

    async def wait(_seconds: int, event: asyncio.Event) -> None:
        event.set()

    with pytest.raises(ValidationError, match="^事务通知邮件未配置$"):
        await module.run_notification_worker(runtime, stop_event=stop, wait=wait)
    assert jobs.claim_calls == []


@pytest.mark.asyncio
async def test_runtime_missing_email_config_never_enters_body_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """direct config 防线必须在 health/claim 前失败，并清理已创建的 engine。"""
    module = _module("apps.notification_worker.runtime")
    disposed: list[bool] = []
    servers: list[bool] = []
    health_started = asyncio.Event()

    class Engine:
        def connect(self):
            class Connection:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                async def execute(self, _statement):
                    return None

            return Connection()

        async def dispose(self) -> None:
            disposed.append(True)

    class Server:
        def __init__(self, _state, _port):
            servers.append(True)
            self.stop = asyncio.Event()

        async def serve(self) -> None:
            health_started.set()
            await self.stop.wait()

        async def wait_started(self) -> None:
            await health_started.wait()

        async def close(self) -> None:
            self.stop.set()

    monkeypatch.setattr(module, "create_engine_from", lambda _url: Engine())
    monkeypatch.setattr(
        module, "assert_database_schema_current", lambda _engine: _async_none()
    )
    monkeypatch.setattr(module, "NotificationHealthServer", Server)
    entered: list[bool] = []

    with pytest.raises(ValidationError, match="^事务通知邮件未配置$"):
        async with module.notification_worker_runtime(_config(email=None)):
            entered.append(True)

    assert entered == []
    assert servers == []
    assert disposed == [True]


@pytest.mark.asyncio
async def test_runtime_context_closes_health_and_disposes_engine_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次 yield 后被取消也必须关闭 health task 与数据库 engine。"""
    module = _module("apps.notification_worker.runtime")
    health_started = asyncio.Event()
    health_closed: list[bool] = []
    disposed: list[bool] = []

    class Engine:
        def connect(self):
            class Connection:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                async def execute(self, _statement):
                    return None

            return Connection()

        async def dispose(self):
            disposed.append(True)

    class Server:
        def __init__(self, _state, _port):
            self.stop = asyncio.Event()

        async def serve(self):
            health_started.set()
            await self.stop.wait()

        async def wait_started(self):
            await health_started.wait()

        async def close(self):
            health_closed.append(True)
            self.stop.set()

    monkeypatch.setattr(module, "create_engine_from", lambda _url: Engine())
    monkeypatch.setattr(module, "assert_database_schema_current", lambda _engine: _async_none())
    monkeypatch.setattr(module, "NotificationHealthServer", Server)
    gmail_closed = _patch_email_composition(monkeypatch, module)

    entered = asyncio.Event()

    async def consume() -> None:
        async with module.notification_worker_runtime(_config()):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(consume())
    await entered.wait()
    await health_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert health_closed == [True]
    assert disposed == [True]
    assert gmail_closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["immediate", "bind"])
async def test_runtime_context_rethrows_health_startup_failure_before_yield(
    monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    """serve 提前退出或绑定失败时 body 不得进入，且 engine 必须释放。"""
    module = _module("apps.notification_worker.runtime")
    health_closed: list[bool] = []
    disposed: list[bool] = []

    class Engine:
        def connect(self):
            class Connection:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                async def execute(self, _statement):
                    return None

            return Connection()

        async def dispose(self):
            disposed.append(True)

    class Server:
        def __init__(self, _state, _port):
            self.waiting = asyncio.Event()

        async def serve(self):
            if failure_mode == "bind":
                await self.waiting.wait()
            raise OSError("private bind failure")

        async def wait_started(self):
            if failure_mode == "bind":
                self.waiting.set()
            await asyncio.Future()

        async def close(self):
            health_closed.append(True)
            self.waiting.set()

    monkeypatch.setattr(module, "create_engine_from", lambda _url: Engine())
    monkeypatch.setattr(module, "assert_database_schema_current", lambda _engine: _async_none())
    monkeypatch.setattr(module, "NotificationHealthServer", Server)
    gmail_closed = _patch_email_composition(monkeypatch, module)
    entered: list[bool] = []

    with pytest.raises(OSError, match="private bind failure"):
        async with module.notification_worker_runtime(_config()):
            entered.append(True)

    assert entered == []
    assert health_closed == [True]
    assert disposed == [True]
    assert gmail_closed == [True]


@pytest.mark.asyncio
async def test_runtime_pre_listening_cancel_finishes_both_health_tasks_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次 listening 前一次 cancel 就必须传播且完成全部清理。"""
    module = _module("apps.notification_worker.runtime")
    serve_entered = asyncio.Event()
    started_entered = asyncio.Event()
    disposed: list[bool] = []
    closed: list[bool] = []

    class Engine:
        def connect(self):
            class Connection:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                async def execute(self, _statement):
                    return None

            return Connection()

        async def dispose(self):
            disposed.append(True)

    class Server:
        def __init__(self, _state, _port):
            self.serve_task: asyncio.Task[object] | None = None
            self.started_task: asyncio.Task[object] | None = None
            servers.append(self)

        async def serve(self):
            self.serve_task = asyncio.current_task()
            serve_entered.set()
            await asyncio.Future()

        async def wait_started(self):
            self.started_task = asyncio.current_task()
            started_entered.set()
            await asyncio.Future()

        async def close(self):
            closed.append(True)

    servers: list[Server] = []

    monkeypatch.setattr(module, "create_engine_from", lambda _url: Engine())
    monkeypatch.setattr(module, "assert_database_schema_current", lambda _engine: _async_none())
    monkeypatch.setattr(module, "NotificationHealthServer", Server)
    gmail_closed = _patch_email_composition(monkeypatch, module)

    async def consume() -> None:
        async with module.notification_worker_runtime(_config()):
            pytest.fail("listening 前取消时 runtime body 不得进入")

    task = asyncio.create_task(consume())
    await serve_entered.wait()
    await started_entered.wait()
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=0.15)
    try:
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            await task
        server = servers[0]
        assert server.serve_task is not None and server.serve_task.done()
        assert server.started_task is not None and server.started_task.done()
        assert closed == [True]
        assert disposed == [True]
        assert gmail_closed == [True]
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _async_none() -> None:
    return None


def test_main_missing_configuration_returns_fixed_nonzero_without_dsn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """入口配置错误不得打印 DSN 或异常原文。"""
    module = _module("apps.notification_worker.main")
    monkeypatch.setattr(module.os, "environ", {})
    with caplog.at_level(logging.ERROR, logger="apps.notification_worker"):
        assert module.main() != 0
    assert "postgresql" not in caplog.text
    assert "database_url" not in caplog.text.casefold()


def test_owner_pending_and_reminder_do_not_select_email_channel():
    from types import SimpleNamespace

    from apps.notification_worker.runtime import NotificationRoutingPolicy
    for reason in ("owner_pending", "owner_reminder"):
        notification = Notification(_TENANT, _EMPLOYEE, NotificationPriority.URGENT, "待接管提醒", NotificationContext(NotificationKind.HANDOFF_ESCALATION,new_id("hand"),new_id("opp"),reason,None), "HandoffEscalationNotice", new_id("key"))
        channels = [SimpleNamespace(name="in_app"), SimpleNamespace(name="email")]
        assert NotificationRoutingPolicy().channels_for(notification, channels) == [channels[0]]
