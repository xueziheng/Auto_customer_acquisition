"""邮件反馈 worker 周期、退避、健康和安全可观测性。"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from shared.errors import TransientError
from shared.schemas.email_feedback import EmailFeedbackPage
from shared.schemas.identifiers import TenantId, new_id
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


class _Reader:
    def __init__(self, pages: list[object]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[object, ...]] = []

    async def fetch(self, *args: object) -> EmailFeedbackPage:
        self.calls.append(args)
        result = self.pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, EmailFeedbackPage)
        return result


class _Processor:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, ...]] = []

    async def process(self, *args: object) -> object:
        self.calls.append(args)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _CursorReader:
    def __init__(self, cursors: list[str | None]) -> None:
        self.cursors = list(cursors)
        self.calls = 0

    async def get_cursor(self, tenant_id: TenantId, mailbox_alias: str) -> str | None:
        del tenant_id, mailbox_alias
        self.calls += 1
        return self.cursors.pop(0)


class _Metrics:
    def __init__(self) -> None:
        self.records: list[tuple[object, dict[str, object]]] = []

    def record(self, name: object, **dimensions: object) -> None:
        self.records.append((name, dimensions))


class _Lease:
    def __init__(self, heartbeats: list[bool] | None = None, acquired: bool = True) -> None:
        self.heartbeats = list(heartbeats or [True, True])
        self.acquired = acquired
        self.closed = False
        self.calls: list[str] = []

    async def acquire(self) -> bool:
        self.calls.append("acquire")
        return self.acquired

    async def heartbeat(self) -> bool:
        self.calls.append("heartbeat")
        return self.heartbeats.pop(0)

    async def close(self) -> None:
        self.calls.append("close")
        self.closed = True


class _PrimaryFailure(BaseException):
    pass


class _HealthFailure(BaseException):
    pass


def _page(start: str | None = None, end: str = "cursor-next") -> EmailFeedbackPage:
    return EmailFeedbackPage(start, end, ())


def _runtime(*, reader: object, processor: object, enabled: bool = True) -> object:
    config_module = importlib.import_module("apps.email_feedback_worker.config")
    health_module = importlib.import_module("apps.email_feedback_worker.health")
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    return runtime_module.EmailFeedbackRuntime(
        lock_engine=object(),
        reader=reader,
        processor=processor,
        config=config_module.EmailFeedbackWorkerConfig(
            TenantId(new_id("tn")),
            "feedback",
            new_id("sid"),
            "feedback-v1",
            poll_interval_seconds=30,
            page_limit=100,
            enabled=enabled,
            health_port=8092,
        ),
        health=health_module.EmailFeedbackHealthState(),
    )


@pytest.mark.asyncio
async def test_one_cycle_uses_heartbeat_cursor_fetch_heartbeat_process_order() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    reader = _Reader([_page()])
    processor = _Processor([SimpleNamespace(
        processed=3,
        duplicates=2,
        quarantined=1,
        hard_bounces=1,
        soft_bounces=1,
        next_cursor="cursor-next",
    )])
    cursor = _CursorReader([None])
    metrics = _Metrics()
    lease = _Lease()
    waits: list[int] = []
    stop = asyncio.Event()

    async def wait(seconds: int, event: asyncio.Event) -> None:
        waits.append(seconds)
        event.set()

    result = await runtime_module.run_email_feedback_worker(
        _runtime(reader=reader, processor=processor),
        cursor_reader=cursor,
        metrics=metrics,
        lock_factory=lambda _engine, _key: lease,
        stop_event=stop,
        wait=wait,
        install_signal_handlers=False,
    )

    assert result.status is runtime_module.WorkerRunStatus.STARTED
    assert result.cycles_completed == 1
    assert lease.calls == ["acquire", "heartbeat", "heartbeat", "close"]
    assert cursor.calls == 1
    assert reader.calls[0][2:] == (None, 100)
    assert processor.calls[0][3] is None
    assert processor.calls[0][4].next_cursor == "cursor-next"
    assert waits == [30]
    assert Counter(name.value for name, _ in metrics.records) == Counter(
        {"processed": 1, "duplicate": 1, "quarantined": 1,
         "hard_bounce": 1, "soft_bounce": 1, "cursor_lag": 1,
         "consecutive_failure": 1}
    )


@pytest.mark.asyncio
async def test_disabled_and_competing_worker_never_fetch() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    for enabled, acquired, expected in (
        (False, True, runtime_module.WorkerRunStatus.DISABLED),
        (True, False, runtime_module.WorkerRunStatus.LOCK_NOT_ACQUIRED),
    ):
        reader = _Reader([_page()])
        lease = _Lease(acquired=acquired)
        result = await runtime_module.run_email_feedback_worker(
            _runtime(reader=reader, processor=_Processor([]), enabled=enabled),
            cursor_reader=_CursorReader([None]),
            metrics=_Metrics(),
            lock_factory=lambda _engine, _key, value=lease: value,
            stop_event=asyncio.Event(),
            install_signal_handlers=False,
        )
        assert result.status is expected
        assert reader.calls == []


@pytest.mark.asyncio
async def test_lock_lost_after_fetch_discards_page_without_processing() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    reader = _Reader([_page()])
    processor = _Processor([])
    lease = _Lease([True, False])

    result = await runtime_module.run_email_feedback_worker(
        _runtime(reader=reader, processor=processor),
        cursor_reader=_CursorReader([None]),
        metrics=_Metrics(),
        lock_factory=lambda _engine, _key: lease,
        stop_event=asyncio.Event(),
        install_signal_handlers=False,
    )

    assert result.status is runtime_module.WorkerRunStatus.LOCK_LOST
    assert len(reader.calls) == 1
    assert processor.calls == []
    assert lease.closed


@pytest.mark.asyncio
async def test_retry_backoff_sequence_and_valid_provider_override() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    failures: list[BaseException] = [
        TransientError("network-customer-secret"),
        ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED, retry_after_seconds=17
        ),
        *[TransientError("temporary") for _ in range(7)],
    ]
    reader = _Reader(failures)
    waits: list[int] = []
    stop = asyncio.Event()

    async def wait(seconds: int, event: asyncio.Event) -> None:
        waits.append(seconds)
        if len(waits) == 9:
            event.set()

    result = await runtime_module.run_email_feedback_worker(
        _runtime(reader=reader, processor=_Processor([])),
        cursor_reader=_CursorReader([None] * 9),
        metrics=_Metrics(),
        lock_factory=lambda _engine, _key: _Lease([True] * 9),
        stop_event=stop,
        wait=wait,
        install_signal_handlers=False,
    )

    assert result.status is runtime_module.WorkerRunStatus.STARTED
    assert waits == [5, 17, 20, 40, 80, 160, 300, 300, 300]


@pytest.mark.asyncio
async def test_success_resets_backoff_only_after_processor_commit() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    reader = _Reader([
        TransientError("fetch"),
        _page(),
        TransientError("fetch-again"),
    ])
    processor = _Processor([SimpleNamespace(
        processed=0, duplicates=0, quarantined=0, hard_bounces=0,
        soft_bounces=0, next_cursor="cursor-next"
    )])
    waits: list[int] = []
    stop = asyncio.Event()

    async def wait(seconds: int, event: asyncio.Event) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            event.set()

    await runtime_module.run_email_feedback_worker(
        _runtime(reader=reader, processor=processor),
        cursor_reader=_CursorReader([None, None, "cursor-next"]),
        metrics=_Metrics(),
        lock_factory=lambda _engine, _key: _Lease([True] * 6),
        stop_event=stop,
        wait=wait,
        install_signal_handlers=False,
    )

    assert waits == [5, 30, 5]


@pytest.mark.asyncio
async def test_stop_set_during_fetch_finishes_current_page_without_next_cycle() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    stop = asyncio.Event()

    class _StoppingReader(_Reader):
        async def fetch(self, *args: object) -> EmailFeedbackPage:
            stop.set()
            return await super().fetch(*args)

    reader = _StoppingReader([_page()])
    processor = _Processor([SimpleNamespace(
        processed=1, duplicates=0, quarantined=0, hard_bounces=0,
        soft_bounces=0, next_cursor="cursor-next"
    )])
    waits: list[int] = []

    async def wait(seconds: int, _event: asyncio.Event) -> None:
        waits.append(seconds)

    result = await runtime_module.run_email_feedback_worker(
        _runtime(reader=reader, processor=processor),
        cursor_reader=_CursorReader([None]),
        metrics=_Metrics(),
        lock_factory=lambda _engine, _key: _Lease([True, True]),
        stop_event=stop,
        wait=wait,
        install_signal_handlers=False,
    )

    assert result.cycles_completed == 1
    assert len(processor.calls) == 1
    assert waits == []


@pytest.mark.asyncio
async def test_signal_handlers_only_set_stop_and_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    callbacks: dict[object, object] = {}
    removed: list[object] = []

    class _Loop:
        def add_signal_handler(self, signum: object, callback: object) -> None:
            callbacks[signum] = callback

        def remove_signal_handler(self, signum: object) -> None:
            removed.append(signum)

    monkeypatch.setattr(runtime_module.asyncio, "get_running_loop", _Loop)
    stop = asyncio.Event()
    cleanup = runtime_module.install_stop_signals(stop)
    assert len(callbacks) == 2
    next(iter(callbacks.values()))()
    assert stop.is_set()
    cleanup()
    assert set(removed) == set(callbacks)


@pytest.mark.asyncio
async def test_external_cancellation_propagates_after_lock_cleanup() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    lease = _Lease([True])
    reader = _Reader([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await runtime_module.run_email_feedback_worker(
            _runtime(reader=reader, processor=_Processor([])),
            cursor_reader=_CursorReader([None]),
            metrics=_Metrics(),
            lock_factory=lambda _engine, _key: lease,
            stop_event=asyncio.Event(),
            install_signal_handlers=False,
        )
    assert lease.closed


@pytest.mark.asyncio
async def test_cleanup_base_exception_never_replaces_primary() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    primary = _PrimaryFailure()

    class _BrokenCleanupLease(_Lease):
        async def close(self) -> None:
            self.closed = True
            raise asyncio.CancelledError

    lease = _BrokenCleanupLease([True])
    reader = _Reader([primary])
    with pytest.raises(_PrimaryFailure) as captured:
        await runtime_module.run_email_feedback_worker(
            _runtime(reader=reader, processor=_Processor([])),
            cursor_reader=_CursorReader([None]),
            metrics=_Metrics(),
            lock_factory=lambda _engine, _key: lease,
            stop_event=asyncio.Event(),
            install_signal_handlers=False,
        )
    assert captured.value is primary
    assert lease.closed


def test_metrics_reject_arbitrary_names() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    expected = {
        "cursor_lag", "processed", "duplicate", "quarantined",
        "hard_bounce", "soft_bounce", "page_rollback", "consecutive_failure",
    }
    assert {item.value for item in runtime_module.EmailFeedbackMetricName} == expected
    with pytest.raises(ValueError):
        runtime_module.EmailFeedbackMetricName("customer-email-secret")


def test_safe_failure_log_excludes_exception_and_customer_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    caplog.set_level(logging.ERROR, logger="apps.email_feedback_worker")
    error = RuntimeError(
        "postgresql://oauth-token@db/customer Alice <secret@example.test>"
    )
    runtime_module.log_worker_failure(
        phase="fetch",
        category="unexpected",
        tenant_id=TenantId(new_id("tn")),
        mailbox_alias="feedback",
        error=error,
    )
    rendered = " ".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )
    for marker in (
        "oauth-token", "secret@example.test", "customer Alice", "postgresql://"
    ):
        assert marker not in rendered
    assert "error_type" in rendered


@pytest.mark.asyncio
async def test_health_only_exposes_fixed_paths_and_provider_degradation_keeps_ready() -> None:
    health_module = importlib.import_module("apps.email_feedback_worker.health")
    state = health_module.EmailFeedbackHealthState()
    app = health_module.create_health_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://health") as client:
        live = await client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "live"}
        assert (await client.get("/health/ready")).status_code == 503
        for checkpoint in ("config", "schema", "database", "registry"):
            state.mark_ready(checkpoint)
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "provider": "ok"}
        state.mark_provider_degraded()
        degraded = await client.get("/health/ready")
        assert degraded.status_code == 200
        assert degraded.json() == {"status": "ready", "provider": "degraded"}
        for path in ("/health/live/", "/health/ready/"):
            trailing_slash = await client.get(path)
            assert trailing_slash.status_code == 404
            assert trailing_slash.json() == {
                "code": "not_found",
                "message": "资源不存在",
            }
        assert (await client.get("/missing")).json() == {
            "code": "not_found", "message": "资源不存在"
        }
        assert (await client.post("/health/live")).json() == {
            "code": "method_not_allowed", "message": "方法不允许"
        }


def test_health_server_binds_fixed_host_port_and_disables_access_log() -> None:
    health_module = importlib.import_module("apps.email_feedback_worker.health")
    server = health_module.EmailFeedbackHealthServer(
        health_module.EmailFeedbackHealthState(), 9123
    )
    assert server.config.host == "0.0.0.0"
    assert server.config.port == 9123
    assert server.config.access_log is False
    assert type(server._server).__name__ == "_NoSignalUvicornServer"


@pytest.mark.asyncio
async def test_application_disabled_serves_health_until_stop_without_worker_io() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    reader = _Reader([_page()])
    runtime = _runtime(reader=reader, processor=_Processor([]), enabled=False)
    trace: list[str] = []
    health_closed = asyncio.Event()

    class _Server:
        async def serve(self) -> None:
            trace.append("health.start")
            await health_closed.wait()

        async def close(self) -> None:
            trace.append("health.close")
            health_closed.set()

    application = runtime_module.EmailFeedbackWorkerApplication(
        runtime=runtime,
        cursor_reader=_CursorReader([None]),
        metrics=_Metrics(),
        health_server=_Server(),
    )
    stop = asyncio.Event()
    stop.set()

    result = await runtime_module.run_email_feedback_application(
        application,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.status is runtime_module.WorkerRunStatus.DISABLED
    assert reader.calls == []
    assert trace == ["health.start", "health.close"]


@pytest.mark.asyncio
async def test_health_failure_stops_disabled_application_and_propagates() -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    runtime = _runtime(reader=_Reader([]), processor=_Processor([]), enabled=False)

    class _Server:
        async def serve(self) -> None:
            await asyncio.sleep(0)
            raise _HealthFailure

        async def close(self) -> None:
            return None

    application = runtime_module.EmailFeedbackWorkerApplication(
        runtime, _CursorReader([]), _Metrics(), _Server()
    )

    with pytest.raises(_HealthFailure):
        await asyncio.wait_for(
            runtime_module.run_email_feedback_application(
                application,
                stop_event=asyncio.Event(),
                install_signal_handlers=False,
            ),
            timeout=1,
        )


@pytest.mark.asyncio
async def test_application_cancellation_during_startup_always_cleans_health_and_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    runtime = _runtime(reader=_Reader([]), processor=_Processor([]), enabled=False)
    original_sleep = asyncio.sleep
    trace: list[str] = []
    health_closed = asyncio.Event()

    class _Server:
        async def serve(self) -> None:
            trace.append("health.serve")
            await health_closed.wait()

        async def close(self) -> None:
            trace.append("health.close")
            health_closed.set()

    def install(_stop: asyncio.Event) -> object:
        trace.append("signals.install")

        def cleanup() -> None:
            trace.append("signals.cleanup")

        return cleanup

    async def cancel_at_first_yield(seconds: float) -> None:
        assert seconds == 0
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await original_sleep(0)

    monkeypatch.setattr(runtime_module, "install_stop_signals", install)
    monkeypatch.setattr(runtime_module.asyncio, "sleep", cancel_at_first_yield)
    application = runtime_module.EmailFeedbackWorkerApplication(
        runtime, _CursorReader([]), _Metrics(), _Server()
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            await runtime_module.run_email_feedback_application(application)
        assert trace == [
            "signals.install",
            "health.serve",
            "signals.cleanup",
            "health.close",
        ]
    finally:
        health_closed.set()
        await original_sleep(0)


@pytest.mark.asyncio
async def test_injected_runtime_factory_owns_application_context() -> None:
    main_module = importlib.import_module("apps.email_feedback_worker.main")
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    entered: list[str] = []
    runtime = _runtime(reader=_Reader([]), processor=_Processor([]), enabled=False)
    health_closed = asyncio.Event()

    class _Server:
        async def serve(self) -> None:
            await health_closed.wait()

        async def close(self) -> None:
            health_closed.set()

    application = runtime_module.EmailFeedbackWorkerApplication(
        runtime, _CursorReader([]), _Metrics(), _Server()
    )

    @asynccontextmanager
    async def context() -> object:
        entered.append("enter")
        try:
            yield application
        finally:
            entered.append("exit")

    stop = asyncio.Event()
    stop.set()
    code = await main_module.run_from_factory(context, stop_event=stop)
    assert code == 0
    assert entered == ["enter", "exit"]


def test_zero_arg_main_fails_closed_on_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    main_module = importlib.import_module("apps.email_feedback_worker.main")
    monkeypatch.setattr(main_module.os, "environ", {})
    caplog.set_level(logging.ERROR, logger="apps.email_feedback_worker")

    assert main_module.main() == 2
    assert [record.getMessage() for record in caplog.records] == [
        "邮件反馈 worker 配置无效"
    ]
