"""S3-11 scheduler worker 集成测试。

覆盖单副本 advisory lock、固定 cycle 顺序、阶段隔离、错误脱敏、优雅停止与
真实 outbox/workflow durable 幂等。测试只注入已完整装配的 runtime；绝不允许
worker 自己构造空 handler registry。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.errors import TradeOSError, TransientError, ValidationError
from shared.events.catalog import OpportunityWon
from shared.schemas.identifiers import EmployeeId, OpportunityId, TenantId
from workflows.engine.runner import StepDefinition, WorkflowDefinition, WorkflowRun


@pytest_asyncio.fixture
async def scheduler_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    """每个测试独立引擎，避免 asyncpg 连接池跨 pytest event loop 复用。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _scheduler() -> Any:
    """RED 阶段把缺失公共能力转成行为失败，避免测试收集错误。"""
    module = importlib.import_module("apps.scheduler_worker.main")
    required = (
        "SchedulerConfig",
        "SchedulerRuntime",
        "WorkerStartStatus",
        "run_scheduler_worker",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.fail(f"RED：scheduler worker 公共能力尚未实现：{missing}")
    return module


class _Drainer:
    def __init__(
        self,
        results: list[int | BaseException],
        order: list[str] | None = None,
    ) -> None:
        self._results = deque(results)
        self.order = order
        self.calls = 0

    async def drain(self) -> int:
        self.calls += 1
        if self.order is not None:
            self.order.append(f"drain:{self.calls}")
        result = self._results.popleft() if self._results else 0
        if isinstance(result, BaseException):
            raise result
        return result


class _Poller:
    def __init__(
        self,
        results: list[int | BaseException],
        order: list[str] | None = None,
        *,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self._results = deque(results)
        self.order = order
        self.on_call = on_call
        self.calls: list[tuple[TenantId, int]] = []

    async def poll_due(self, tenant_id: TenantId, limit: int) -> int:
        self.calls.append((tenant_id, limit))
        if self.order is not None:
            self.order.append(f"workflow:{len(self.calls)}")
        if self.on_call is not None:
            self.on_call()
        result = self._results.popleft() if self._results else 0
        if isinstance(result, BaseException):
            raise result
        return result


class _BlockingDrainer:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def drain(self) -> int:
        self.entered.set()
        await asyncio.Event().wait()
        return 0


def _config(module: Any, *, lock_key: int, interval: float = 0.01) -> Any:
    return module.SchedulerConfig(
        interval_seconds=interval,
        batch_limit=8,
        lock_key=lock_key,
    )


def _runtime(
    module: Any,
    lock_engine: AsyncEngine,
    tenant: TenantId,
    drainer: object,
    poller: object,
    *,
    lock_key: int,
    activation: object | None = None,
) -> Any:
    return module.SchedulerRuntime(
        lock_engine=lock_engine,
        outbox=drainer,
        workflow=poller,
        tenant_id=tenant,
        config=_config(module, lock_key=lock_key),
        activation=activation,
    )


class _RecordingActivation:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls = 0

    async def activate(self) -> None:
        self.calls += 1
        self.order.append("runtime_composed")


class _FailingActivation:
    def __init__(self, message: str = "activation-sensitive-detail") -> None:
        self.message = message
        self.calls = 0

    async def activate(self) -> None:
        self.calls += 1
        raise TransientError(self.message)


def _stop_after_waits(
    count: int,
) -> Callable[[float, asyncio.Event], Awaitable[None]]:
    calls = 0

    async def wait(interval: float, stop_event: asyncio.Event) -> None:
        nonlocal calls
        assert interval > 0
        calls += 1
        if calls >= count:
            stop_event.set()

    return wait


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"interval_seconds": 0}, "interval"),
        ({"interval_seconds": -1}, "interval"),
        ({"interval_seconds": float("nan")}, "interval"),
        ({"interval_seconds": float("inf")}, "interval"),
        ({"interval_seconds": float("-inf")}, "interval"),
        ({"batch_limit": 0}, "batch"),
        ({"batch_limit": True}, "batch"),
        ({"lock_key": True}, "lock"),
        ({"lock_key": -(2**63) - 1}, "lock"),
        ({"lock_key": 2**63}, "lock"),
    ],
)
def test_config_rejects_invalid_values(
    kwargs: dict[str, object], message: str
) -> None:
    module = _scheduler()
    values: dict[str, object] = {
        "interval_seconds": 1,
        "batch_limit": 10,
        "lock_key": 1,
    }
    values.update(kwargs)

    with pytest.raises(ValidationError, match=message):
        module.SchedulerConfig(**values)


def test_zero_arg_main_fails_closed_without_runtime_or_database(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _scheduler()
    secret_dsn = "postgresql+asyncpg://user:secret@example.invalid/db"
    monkeypatch.setenv("DATABASE_URL", secret_dsn)

    def forbidden_run(coro: object) -> int:
        del coro
        pytest.fail("zero-arg main 不得创建 coroutine 或连接数据库")

    monkeypatch.setattr(asyncio, "run", forbidden_run)
    caplog.set_level(logging.ERROR, logger="apps.scheduler_worker.main")

    assert module.main() != 0
    assert "runtime 未配置" in caplog.text
    assert secret_dsn not in caplog.text
    assert "secret" not in caplog.text


def test_main_returns_nonzero_when_runtime_loses_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _scheduler()

    @asynccontextmanager
    async def runtime_factory() -> AsyncIterator[Any]:
        yield object()

    async def lock_lost(runtime: object) -> Any:
        del runtime
        return module.WorkerRunResult(module.WorkerStartStatus.LOCK_LOST, 1)

    monkeypatch.setattr(module, "run_scheduler_worker", lock_lost)

    assert module.main(runtime_factory) != 0


async def test_real_postgres_lock_allows_only_one_worker_and_releases(
    scheduler_db: AsyncEngine,
) -> None:
    module = _scheduler()
    tenant = TenantId("scheduler-lock-tenant")
    lock_key = 3_110_001
    first_drainer = _BlockingDrainer()
    first = _runtime(
        module,
        scheduler_db,
        tenant,
        first_drainer,
        _Poller([0]),
        lock_key=lock_key,
    )
    first_task = asyncio.create_task(
        module.run_scheduler_worker(
            first,
            install_signal_handlers=False,
        )
    )
    await asyncio.wait_for(first_drainer.entered.wait(), timeout=2)

    second_drainer = _Drainer([0])
    second_poller = _Poller([0])
    second = _runtime(
        module,
        scheduler_db,
        tenant,
        second_drainer,
        second_poller,
        lock_key=lock_key,
    )
    result = await module.run_scheduler_worker(
        second,
        wait=_stop_after_waits(1),
        install_signal_handlers=False,
    )
    assert result.status is module.WorkerStartStatus.NOT_STARTED
    assert result.cycles_completed == 0
    assert second_drainer.calls == 0
    assert second_poller.calls == []

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    third = _runtime(
        module,
        scheduler_db,
        tenant,
        _Drainer([0]),
        _Poller([0]),
        lock_key=lock_key,
    )
    restarted = await module.run_scheduler_worker(
        third,
        wait=_stop_after_waits(1),
        install_signal_handlers=False,
    )
    assert restarted.status is module.WorkerStartStatus.STARTED
    assert restarted.cycles_completed == 1


async def test_runtime_activation_runs_after_lock_and_before_first_cycle(
    scheduler_db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _scheduler()
    order: list[str] = []
    stop = asyncio.Event()
    activation = _RecordingActivation(order)
    original_heartbeat = module._same_lock_backend

    async def recording_heartbeat(connection: object, expected_pid: int) -> bool:
        confirmed = await original_heartbeat(connection, expected_pid)
        if confirmed:
            order.append("lock_confirmed")
        return confirmed

    monkeypatch.setattr(module, "_same_lock_backend", recording_heartbeat)
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-runtime-activation"),
        _Drainer([0], order),
        _Poller([0], on_call=stop.set),
        lock_key=3_110_002,
        activation=activation,
    )

    result = await module.run_scheduler_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.status is module.WorkerStartStatus.STARTED
    assert activation.calls == 1
    assert order[:3] == ["lock_confirmed", "runtime_composed", "drain:1"]


async def test_activation_failure_releases_lock_and_runs_no_cycle(
    scheduler_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _scheduler()
    lock_key = 3_110_003
    activation = _FailingActivation()
    drainer = _Drainer([0])
    poller = _Poller([0])
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-activation-failure"),
        drainer,
        poller,
        lock_key=lock_key,
        activation=activation,
    )
    caplog.set_level(logging.ERROR, logger="apps.scheduler_worker.main")

    with pytest.raises(TransientError, match="activation-sensitive-detail"):
        await module.run_scheduler_worker(runtime, install_signal_handlers=False)

    assert activation.calls == 1
    assert drainer.calls == 0
    assert poller.calls == []
    assert "activation-sensitive-detail" not in caplog.text
    async with scheduler_db.connect() as connection:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        ).scalar_one()
        assert acquired is True
        released = (
            await connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lock_key},
            )
        ).scalar_one()
        assert released is True


async def test_worker_without_lock_never_writes_runtime_composed(
    scheduler_db: AsyncEngine,
) -> None:
    module = _scheduler()
    lock_key = 3_110_004
    first_drainer = _BlockingDrainer()
    first = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-runtime-owner"),
        first_drainer,
        _Poller([0]),
        lock_key=lock_key,
    )
    owner = asyncio.create_task(
        module.run_scheduler_worker(first, install_signal_handlers=False)
    )
    await asyncio.wait_for(first_drainer.entered.wait(), timeout=2)
    activation = _RecordingActivation([])
    contender = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-runtime-contender"),
        _Drainer([0]),
        _Poller([0]),
        lock_key=lock_key,
        activation=activation,
    )

    result = await module.run_scheduler_worker(
        contender,
        install_signal_handlers=False,
    )

    assert result.status is module.WorkerStartStatus.NOT_STARTED
    assert activation.calls == 0
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner


async def _lock_holder(
    engine: AsyncEngine, lock_key: int
) -> tuple[int, str]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT l.pid, a.state FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.granted "
                    "AND l.classid::bigint = 0 AND l.objid::bigint = :lock_key "
                    "AND l.objsubid = 1"
                ),
                {"lock_key": lock_key},
            )
        ).one()
        return (int(row.pid), str(row.state))


async def test_lock_connection_commits_and_is_not_idle_in_transaction(
    scheduler_db: AsyncEngine,
) -> None:
    module = _scheduler()
    lock_key = 3_110_005
    drainer = _BlockingDrainer()
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-lock-transaction-state"),
        drainer,
        _Poller([0]),
        lock_key=lock_key,
    )
    task = asyncio.create_task(
        module.run_scheduler_worker(runtime, install_signal_handlers=False)
    )
    await asyncio.wait_for(drainer.entered.wait(), timeout=2)
    _, state = await _lock_holder(scheduler_db, lock_key)
    assert state == "idle"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_terminated_lock_backend_stops_before_next_cycle(
    scheduler_db: AsyncEngine,
) -> None:
    module = _scheduler()
    lock_key = 3_110_006
    drainer = _Drainer([1, 1])
    poller = _Poller([0, 0])
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-lock-loss"),
        drainer,
        poller,
        lock_key=lock_key,
    )
    wait_calls = 0

    async def terminate_holder(
        interval: float, stop_event: asyncio.Event
    ) -> None:
        nonlocal wait_calls
        del interval
        wait_calls += 1
        if wait_calls > 1:
            stop_event.set()
            return
        pid, _ = await _lock_holder(scheduler_db, lock_key)
        async with scheduler_db.begin() as connection:
            terminated = (
                await connection.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                )
            ).scalar_one()
        assert terminated is True

    result = await module.run_scheduler_worker(
        runtime,
        wait=terminate_holder,
        install_signal_handlers=False,
    )

    assert result.status is module.WorkerStartStatus.LOCK_LOST
    assert result.cycles_completed == 1
    assert drainer.calls == 1
    assert len(poller.calls) == 1


@pytest.mark.parametrize(
    ("workflow_count", "expected"),
    [
        (1, ["drain:1", "workflow:1", "drain:2"]),
        (0, ["drain:1", "workflow:1"]),
    ],
)
async def test_cycle_order_and_conditional_post_drain(
    scheduler_db: AsyncEngine,
    workflow_count: int,
    expected: list[str],
) -> None:
    module = _scheduler()
    stop = asyncio.Event()
    order: list[str] = []
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId(f"scheduler-order-{workflow_count}"),
        _Drainer([7, 11], order),
        _Poller([workflow_count], order, on_call=stop.set),
        lock_key=3_110_010 + workflow_count,
    )

    result = await module.run_scheduler_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.status is module.WorkerStartStatus.STARTED
    assert result.cycles_completed == 1
    assert order == expected


async def test_phase_failure_isolated_and_next_cycle_continues(
    scheduler_db: AsyncEngine,
) -> None:
    module = _scheduler()
    order: list[str] = []
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-phase-isolation"),
        _Drainer([3, 5, 7], order),
        _Poller([ValidationError("do-not-log"), 1], order),
        lock_key=3_110_020,
    )

    result = await module.run_scheduler_worker(
        runtime,
        wait=_stop_after_waits(2),
        install_signal_handlers=False,
    )

    assert result.cycles_completed == 2
    assert order == [
        "drain:1",
        "workflow:1",
        "drain:2",
        "workflow:2",
        "drain:3",
    ]


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TransientError("transient-secret"), "transient"),
        (ValidationError("permanent-secret"), "permanent"),
        (RuntimeError("unexpected-secret"), "unexpected"),
    ],
)
async def test_error_classification_and_logs_are_redacted(
    scheduler_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
    category: str,
) -> None:
    module = _scheduler()
    stop = asyncio.Event()
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId(f"scheduler-errors-{category}"),
        _Drainer([error]),
        _Poller([0], on_call=stop.set),
        lock_key={
            "transient": 3_110_031,
            "permanent": 3_110_032,
            "unexpected": 3_110_033,
        }[category],
    )
    caplog.set_level(logging.INFO, logger="apps.scheduler_worker.main")

    result = await module.run_scheduler_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.cycles_completed == 1
    phase_records = [
        record
        for record in caplog.records
        if getattr(record, "scheduler_phase", None) == "outbox_pre"
    ]
    assert len(phase_records) == 1
    assert phase_records[0].__dict__["error_category"] == category
    assert "secret" not in caplog.text
    assert error.args[0] not in caplog.text
    assert phase_records[0].exc_info is None


class _RetryableCustomError(TradeOSError):
    is_retryable = True


async def test_retryable_flag_drives_classification_for_all_tradeos_errors(
    scheduler_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _scheduler()
    stop = asyncio.Event()
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-custom-retryable"),
        _Drainer([_RetryableCustomError("hidden")]),
        _Poller([0], on_call=stop.set),
        lock_key=3_110_034,
    )
    caplog.set_level(logging.INFO, logger="apps.scheduler_worker.main")

    await module.run_scheduler_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    record = next(
        row
        for row in caplog.records
        if getattr(row, "scheduler_phase", None) == "outbox_pre"
    )
    assert record.__dict__["error_category"] == "transient"


async def test_stop_event_finishes_inflight_cycle_without_wait_or_cancellation(
    scheduler_db: AsyncEngine,
) -> None:
    module = _scheduler()
    stop = asyncio.Event()
    order: list[str] = []

    async def forbidden_wait(interval: float, event: asyncio.Event) -> None:
        del interval, event
        pytest.fail("cycle 内收到 stop 后不得再进入 interval wait")

    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-graceful-stop"),
        _Drainer([1, 2], order),
        _Poller([1], order, on_call=stop.set),
        lock_key=3_110_040,
    )

    result = await module.run_scheduler_worker(
        runtime,
        stop_event=stop,
        wait=forbidden_wait,
        install_signal_handlers=False,
    )

    assert result.cycles_completed == 1
    assert order == ["drain:1", "workflow:1", "drain:2"]


async def test_stop_during_heartbeat_does_not_start_a_new_cycle(
    scheduler_db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _scheduler()
    stop = asyncio.Event()
    lock_key = 3_110_045
    drainer = _Drainer([1])
    poller = _Poller([1])
    runtime = _runtime(
        module,
        scheduler_db,
        TenantId("scheduler-stop-during-heartbeat"),
        drainer,
        poller,
        lock_key=lock_key,
    )

    async def heartbeat_then_stop(connection: object, expected_pid: int) -> bool:
        del connection, expected_pid
        await asyncio.sleep(0)
        stop.set()
        return True

    monkeypatch.setattr(module, "_same_lock_backend", heartbeat_then_stop)

    result = await module.run_scheduler_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.status is module.WorkerStartStatus.STARTED
    assert result.cycles_completed == 0
    assert drainer.calls == 0
    assert poller.calls == []
    async with scheduler_db.connect() as connection:
        reacquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        ).scalar_one()
        assert reacquired is True
        released = (
            await connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lock_key},
            )
        ).scalar_one()
        assert released is True


async def test_cancellation_propagates_and_releases_real_lock(
    scheduler_db: AsyncEngine,
) -> None:
    module = _scheduler()
    tenant = TenantId("scheduler-cancel")
    lock_key = 3_110_050
    drainer = _BlockingDrainer()
    runtime = _runtime(
        module,
        scheduler_db,
        tenant,
        drainer,
        _Poller([0]),
        lock_key=lock_key,
    )
    task = asyncio.create_task(
        module.run_scheduler_worker(runtime, install_signal_handlers=False)
    )
    await asyncio.wait_for(drainer.entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with scheduler_db.connect() as connection:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        ).scalar_one()
        assert acquired is True
        released = (
            await connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lock_key},
            )
        ).scalar_one()
        assert released is True


class _CountingEventHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, event: OpportunityWon) -> None:
        del event
        self.calls += 1


class _CompletingStep:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, object]]:
        del run
        self.calls += 1
        return ("complete", None, {})


async def _delete_tenant_data(engine: AsyncEngine, tenant: TenantId) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = :tenant"),
            {"tenant": str(tenant)},
        )
        await connection.execute(
            text("DELETE FROM workflow_runs WHERE tenant_id = :tenant"),
            {"tenant": str(tenant)},
        )


async def test_repeated_cycles_do_not_repeat_real_outbox_or_workflow(
    scheduler_db: AsyncEngine,
) -> None:
    from infra.db.outbox import PostgresEventBus
    from infra.db.outbox_delivery import OutboxDeliverer
    from infra.db.workflow_engine import PostgresWorkflowEngine

    module = _scheduler()
    tenant = TenantId("scheduler-real-durable")
    factory = async_sessionmaker(bind=scheduler_db, expire_on_commit=False)
    await _delete_tenant_data(scheduler_db, tenant)
    step = _CompletingStep()
    workflow = PostgresWorkflowEngine(factory, {"test.complete": step})
    workflow.register(
        WorkflowDefinition(
            workflow_type="scheduler_idempotency",
            version=1,
            steps=(StepDefinition("only", "test.complete"),),
        )
    )
    await workflow.start(
        tenant,
        "scheduler_idempotency",
        "subject-1",
        {},
        "scheduler-idempotency-key",
    )
    event_handler = _CountingEventHandler()
    outbox = OutboxDeliverer(factory, tenant)
    outbox.register_handler(
        OpportunityWon,
        "scheduler.test.opportunity_won",
        cast(Any, event_handler),
    )
    session = factory()
    try:
        bus = PostgresEventBus(session, tenant)
        await bus.publish(
            OpportunityWon(
                tenant_id=tenant,
                occurred_at=datetime.now(UTC),
                opportunity_id=OpportunityId("scheduler-opp"),
                closed_by=EmployeeId("scheduler-human"),
            )
        )
        await session.commit()
    finally:
        await session.close()

    runtime = module.SchedulerRuntime(
        lock_engine=scheduler_db,
        outbox=outbox,
        workflow=workflow,
        tenant_id=tenant,
        config=_config(module, lock_key=3_110_060),
    )
    result = await module.run_scheduler_worker(
        runtime,
        wait=_stop_after_waits(2),
        install_signal_handlers=False,
    )

    assert result.cycles_completed == 2
    assert event_handler.calls == 1
    assert step.calls == 1
    async with scheduler_db.connect() as connection:
        event_status = (
            await connection.execute(
                text(
                    "SELECT status FROM outbox_events "
                    "WHERE tenant_id = :tenant"
                ),
                {"tenant": str(tenant)},
            )
        ).scalar_one()
        run_status = (
            await connection.execute(
                text(
                    "SELECT status FROM workflow_runs "
                    "WHERE tenant_id = :tenant"
                ),
                {"tenant": str(tenant)},
            )
        ).scalar_one()
    assert event_status == "delivered"
    assert run_status == "completed"


def test_task4_scheduler_config_is_strict_and_redacts_database_url() -> None:
    """生产配置缺字段失败关闭，repr/错误不暴露 DSN。"""
    try:
        config_module = importlib.import_module("apps.scheduler_worker.config")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：scheduler config 尚未创建（{exc.name}）")
    secret_dsn = "postgresql+asyncpg://user:secret@example.invalid/db"
    environ = {
        "DATABASE_URL": secret_dsn,
        "TRADEOS_TENANT_ID": "tn_01K2C5R6J7ABCDEFGHJKMNPQRS",
        "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
        "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
        "TRADEOS_SCHEDULER_LOCK_KEY": "3110001",
        "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "3",
        "TRADEOS_HANDOFF_T1_SECONDS": "3600",
        "TRADEOS_HANDOFF_T2_SECONDS": "7200",
        "TRADEOS_DKIM_SELECTOR": "s1",
        "TRADEOS_SCHEDULER_HEALTH_PORT": "8094",
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "SCHEDULER_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_REF",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "route-scheduler",
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsub.example",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "k1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": '{"k1": "UNSUBSCRIBE_HMAC_CURRENT"}',
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
    }
    config = config_module.SchedulerWorkerConfig.from_environ(environ)
    assert config.dkim_selector == "s1"
    assert secret_dsn not in repr(config)
    assert "secret" not in repr(config)
    for missing in environ:
        broken = dict(environ)
        broken.pop(missing)
        with pytest.raises(Exception) as caught:
            config_module.SchedulerWorkerConfig.from_environ(broken)
        assert secret_dsn not in str(caught.value)
        assert "secret" not in str(caught.value)

    lease_too_long = dict(environ)
    lease_too_long["TRADEOS_TOOL_LEASE_SECONDS"] = "121"
    with pytest.raises(ValidationError):
        config_module.SchedulerWorkerConfig.from_environ(lease_too_long)


def test_slice4_scheduler_environment_helpers_satisfy_strict_config() -> None:
    """E2E 与演示的真实 helper 产物必须包含显式 Hunter 禁用事实。"""
    config_module = importlib.import_module("apps.scheduler_worker.config")
    e2e_module = importlib.import_module("tests.e2e.test_slice4_manual_send")
    demo_module = importlib.import_module("scripts.demo_slice4_manual_send")
    identifiers = importlib.import_module("shared.schemas.identifiers")
    database_url = "postgresql+asyncpg://localhost:5432/test"
    tenant = identifiers.TenantId(identifiers.new_id("tn"))
    e2e_env = e2e_module._runtime_env(
        database_url,
        tenant,
        "http://127.0.0.1:4173",
        "http://127.0.0.1:8089",
        boss=identifiers.EmployeeId(identifiers.new_id("emp")),
        campaign=identifiers.CampaignId(identifiers.new_id("cmp")),
        approval=identifiers.ApprovalId(identifiers.new_id("apr")),
        identity=identifiers.SendingIdentityId(identifiers.new_id("sid")),
        contact=identifiers.ContactPointId(identifiers.new_id("cp")),
        account=identifiers.ProspectAccountId(identifiers.new_id("acc")),
    )
    e2e_env.update(
        {
            "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
            "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
            "TRADEOS_SCHEDULER_LOCK_KEY": "3110002",
            "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "7",
            "TRADEOS_HANDOFF_T1_SECONDS": "2",
            "TRADEOS_HANDOFF_T2_SECONDS": "2",
            "TRADEOS_DKIM_SELECTOR": "s1",
            "TRADEOS_SCHEDULER_HEALTH_PORT": "8094",
            "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
        }
    )
    demo_env = demo_module._scheduler_env(database_url, tenant)

    for environ in (e2e_env, demo_env):
        config = config_module.SchedulerWorkerConfig.from_environ(environ)
        assert config.hunter_contacts.enabled is False
        assert config.hunter_contacts.configuration is None
        assert config.hunter_contacts.secret_ref is None


async def test_dns_read_rate_limit_enforces_bounded_window() -> None:
    """rate_limit stage 必须真实限流，不能只检查 prepared 存在。"""
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    limiter = runtime_module._DnsReadRateLimitCheck(
        max_calls=2,
        window_seconds=60,
        now=lambda: datetime(2026, 8, 14, 12, tzinfo=UTC),
    )
    context = type(
        "Context",
        (),
        {"tenant_id": TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")},
    )()
    state = type("State", (), {"prepared": object()})()

    assert await limiter.check(context, state) is None
    assert await limiter.check(context, state) is None
    with pytest.raises(Exception) as caught:
        await limiter.check(context, state)
    category = getattr(caught.value, "category", None)
    assert category is not None
    assert category.value == "rate_limited"
    assert getattr(caught.value, "retry_after_seconds", None) == 60


def test_complete_registry_has_handoff_all_notification_and_auth_handlers() -> None:
    """partial registry 会把别的订阅先标 delivered，必须一次装配完整集合。"""
    try:
        runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：scheduler production runtime 尚未创建（{exc.name}）")

    class Engine:
        def __init__(self) -> None:
            self.definitions: list[Any] = []

        def register(self, definition) -> None:
            self.definitions.append(definition)

    class Registry:
        def __init__(self) -> None:
            self.handlers: list[tuple[str, str]] = []

        def register_handler(self, event_type, handler_name, handler) -> None:
            del handler
            self.handlers.append((event_type.__name__, handler_name))

    engine = Engine()
    registry = Registry()
    runtime_module.register_complete_scheduler(
        engine,
        registry,
        notification_handler=object(),
        t1=datetime.resolution * 3_600_000_000,
        t2=datetime.resolution * 7_200_000_000,
    )
    assert {item.workflow_type for item in engine.definitions} == {
        "human_handoff",
        "sending_identity_authentication",
    }
    assert set(registry.handlers) == {
        ("HandoffRequested", "human_handoff.requested"),
        ("HandoffAccepted", "human_handoff.accepted"),
        ("HandoffRequested", "notification.handoff_requested"),
        ("HandoffQueueBacklogged", "notification.handoff_queue_backlogged"),
        ("SendingIdentitySuspended", "notification.sending_identity_suspended"),
        (
            "ReputationThresholdBreached",
            "notification.reputation_threshold_breached",
        ),
        ("CommitmentOverdue", "notification.commitment_overdue"),
        ("ApprovalDecided", "notification.approval_decided"),
        ("AuthenticationCheckRequested", "sending_identity_auth.requested"),
    }
    assert all(
        name != "playbook_change.approval_decided" for _, name in registry.handlers
    )


def test_scheduler_rejects_partial_playbook_change_composition() -> None:
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    organization = importlib.import_module("domains.organization.permissions")

    class Approvals:
        async def submit(self, *args, **kwargs):
            del args, kwargs

        async def get(self, *args, **kwargs):
            del args, kwargs

        async def expire_overdue(self, *args, **kwargs):
            del args, kwargs

        async def mark_applied(self, *args, **kwargs):
            del args, kwargs

        async def mark_apply_failed(self, *args, **kwargs):
            del args, kwargs

    actor = organization.OrganizationActor(
        "system:playbook-change",
        organization.OrganizationScope(
            organization.OrganizationScopeLevel.SYSTEM,
            TenantId("tenant-playbook-composition"),
        ),
        "system",
    )
    with pytest.raises(ValidationError, match="Playbook 变更依赖未完整配置"):
        runtime_module.PlaybookChangeComposition(
            organization=object(), approvals=Approvals(), system_actor=actor
        )


def test_scheduler_exports_concrete_production_composition_and_health() -> None:
    """Task 4 不能只留下接收预制 engine/outbox 的薄 wrapper。"""
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    for symbol in (
        "SchedulerDomainDependencies",
        "SchedulerRuntimeFactory",
        "SchedulerHealthState",
        "SchedulerHealthServer",
    ):
        assert hasattr(runtime_module, symbol), f"RED：缺少生产装配 {symbol}"


class _FactoryOpportunity:
    async def record_handoff_escalation(self, *args, **kwargs):
        del args, kwargs


class _FactoryEmployees:
    async def get_employee(self, *args, **kwargs):
        del args, kwargs

    async def resolve_owner(self, *args, **kwargs):
        del args, kwargs


class _FactoryAudience:
    async def recipients_for(self, tenant_id, event):
        del tenant_id, event
        return ()


class _FactoryResolver:
    async def resolve(self, name, rdtype):
        del name, rdtype
        pytest.fail("composition 不得触发真实 DNS")


class _FactoryHealthServer:
    def __init__(self, state, port):
        self.state = state
        self.port = port
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def serve(self):
        self.started.set()
        await self.closed.wait()

    async def wait_started(self):
        await self.started.wait()

    async def close(self):
        self.closed.set()


class _FactoryCampaignFacts:
    async def get_contact_eligibility(self, *args, **kwargs):
        del args, kwargs

    async def get_sending_identity_eligibility(self, *args, **kwargs):
        del args, kwargs

    async def get_campaign_approval(self, *args, **kwargs):
        del args, kwargs

    async def get_reply_status(self, *args, **kwargs):
        del args, kwargs


class _FactoryMaterials:
    async def resolve(self, *args, **kwargs):
        del args, kwargs


class _FactoryGmailSecrets:
    def resolve(self, secret_ref: str) -> str:
        del secret_ref
        raise AssertionError("runtime 装配不得解析 Gmail 凭证")


class _FactoryGmailTransport:
    async def search(self, **kwargs):
        del kwargs

    async def send(self, **kwargs):
        del kwargs


class _FactoryTaskReader:
    async def load(self, *args, **kwargs):
        del args, kwargs


class _FactoryCapability:
    async def run(self, *args, **kwargs):
        del args, kwargs


class _FactoryProspecting:
    async def resolve_account(self, *args, **kwargs):
        del args, kwargs

    async def record_discovered_contact(self, *args, **kwargs):
        del args, kwargs

    async def get_contact_point(self, *args, **kwargs):
        del args, kwargs


class _FactoryActorResolver:
    async def resolve(self, *args, **kwargs):
        del args, kwargs


class _FactoryDiscoveryPolicy:
    async def preflight(self, *args, **kwargs):
        del args, kwargs


class _NoIoHunterTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, path, params, *, api_key):
        del path, params, api_key
        self.calls += 1
        raise AssertionError("runtime 装配不得调用 Hunter")


class _TrackingEnvironmentSecrets:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, secret_ref: str) -> str:
        self.calls.append(secret_ref)
        if secret_ref == "HUNTER_API_KEY_REF":
            raise AssertionError("runtime 装配不得解析 Hunter 凭证")
        values = {
            "SCHEDULER_FINGERPRINT_KEY": "f" * 32,
            "UNSUBSCRIBE_HMAC_CURRENT": "u" * 32,
        }
        return values[secret_ref]


def _factory_environ(
    db_url: str,
    tenant: TenantId,
    *,
    hunter_enabled: bool,
) -> dict[str, str]:
    environ = {
        "DATABASE_URL": db_url,
        "TRADEOS_TENANT_ID": str(tenant),
        "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
        "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
        "TRADEOS_SCHEDULER_LOCK_KEY": "3110090",
        "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "7",
        "TRADEOS_HANDOFF_T1_SECONDS": "3600",
        "TRADEOS_HANDOFF_T2_SECONDS": "7200",
        "TRADEOS_DKIM_SELECTOR": "s1",
        "TRADEOS_SCHEDULER_HEALTH_PORT": "8094",
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "SCHEDULER_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_REF",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "route-scheduler",
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsub.example",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "k1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
            '{"k1": "UNSUBSCRIBE_HMAC_CURRENT"}'
        ),
        "SCHEDULER_FINGERPRINT_KEY": "f" * 32,
        "UNSUBSCRIBE_HMAC_CURRENT": "u" * 32,
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "true" if hunter_enabled else "false",
    }
    if hunter_enabled:
        environ.update(
            {
                "TRADEOS_HUNTER_CONFIGURATION_VERSION": "config-v1",
                "TRADEOS_HUNTER_API_KEY_SECRET_REF": "HUNTER_API_KEY_REF",
                "TRADEOS_HUNTER_API_KEY_VERSION": "key-v1",
            }
        )
    return environ


def _factory_dependencies(runtime_module: Any, *, with_hunter: bool) -> Any:
    if not with_hunter:
        return runtime_module.SchedulerDomainDependencies(
            _FactoryOpportunity(), _FactoryEmployees(), _FactoryAudience()
        )
    facts = _FactoryCampaignFacts()
    campaign = runtime_module.CampaignMessagingComposition(
        contact_eligibility=facts,
        sending_identity_eligibility=facts,
        campaign_approvals=facts,
        reply_status=facts,
        delivery_materials=_FactoryMaterials(),
        secret_resolver=_FactoryGmailSecrets(),
        gmail_transport=_FactoryGmailTransport(),
    )
    prospecting = _FactoryProspecting()
    account = runtime_module.AccountDiscoveryComposition(
        task_reader=_FactoryTaskReader(),
        capability=_FactoryCapability(),
        prospecting=prospecting,
        employees=_FactoryEmployees(),
        actor_resolver=_FactoryActorResolver(),
        hunter=runtime_module.HunterContactComposition(
            discovery_policy=_FactoryDiscoveryPolicy()
        ),
    )
    return runtime_module.SchedulerDomainDependencies(
        _FactoryOpportunity(),
        _FactoryEmployees(),
        _FactoryAudience(),
        campaign_messaging=campaign,
        account_discovery=account,
    )


async def _seed_hunter_readiness(
    db_url: str,
    tenant: TenantId,
    state: str,
) -> object:
    readiness = importlib.import_module("tool_gateway.provider_readiness")
    uow_module = importlib.import_module("infra.db.provider_readiness_uow")
    session_module = importlib.import_module("infra.db.session")
    engine = session_module.create_engine_from(db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        actor = readiness.ProviderReadinessActor(
            "system:test-hunter",
            tenant,
            frozenset(readiness.ProviderReadinessPermission),
        )
        service = readiness.ProviderReadinessServiceImpl(
            lambda requested: uow_module.SqlAlchemyProviderReadinessUnitOfWork(
                factory, requested
            ),
            runtime_actor=actor,
            now=lambda: datetime(2026, 8, 25, 12, tzinfo=UTC),
        )
        configuration = readiness.ProviderConfiguration.hunter_contacts(
            "config-v1", "key-v1"
        )
        await service.declare_configuration(
            tenant,
            configuration,
            actor=actor,
            idempotency_key="configure:config-v1",
        )
        if state in {"passed", "ready"}:
            await service.mark_validation_started(
                tenant,
                configuration.configuration_hash,
                validation_key="validate:config-v1",
                actor=actor,
            )
            await service.mark_validation_passed(
                tenant,
                configuration.configuration_hash,
                validation_key="validate:config-v1",
                evidence_ref="tool-call:validate-config-v1",
                actor=actor,
            )
        if state == "ready":
            runtime_actor = readiness.ProviderReadinessActor(
                "system:scheduler-hunter",
                tenant,
                frozenset({readiness.ProviderReadinessPermission.COMPOSE}),
            )
            await service.mark_runtime_composed(
                tenant,
                configuration.configuration_hash,
                actor=runtime_actor,
                idempotency_key=(
                    "hunter-runtime:config-v1:"
                    f"{configuration.connector_profile_version}"
                ),
            )
        return configuration
    finally:
        await engine.dispose()


async def _hunter_readiness_events(
    db_url: str,
    tenant: TenantId,
) -> list[object]:
    readiness = importlib.import_module("tool_gateway.provider_readiness")
    uow_module = importlib.import_module("infra.db.provider_readiness_uow")
    session_module = importlib.import_module("infra.db.session")
    engine = session_module.create_engine_from(db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with uow_module.SqlAlchemyProviderReadinessUnitOfWork(
            factory, tenant
        ) as uow:
            return await uow.readiness.list_events(
                tenant,
                readiness.ProviderId.HUNTER,
                readiness.HUNTER_CONTACT_CAPABILITIES,
            )
    finally:
        await engine.dispose()


async def test_hunter_disabled_builds_without_contact_tools_or_activation(
    db_url: str,
) -> None:
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    tenant = TenantId("tn_01M0VKA9S6KX7HRBG3G3ETYNB7")
    transport_calls = 0

    def forbidden_transport() -> _NoIoHunterTransport:
        nonlocal transport_calls
        transport_calls += 1
        return _NoIoHunterTransport()

    factory = runtime_module.SchedulerRuntimeFactory(
        _factory_environ(db_url, tenant, hunter_enabled=False),
        _factory_dependencies(runtime_module, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
        hunter_transport_factory=forbidden_transport,
    )

    async with factory() as runtime:
        assert runtime.activation is None
        assert not any(
            name.startswith("account_discovery.")
            for name in runtime.workflow._handlers
        )
    assert transport_calls == 0


async def test_hunter_enabled_without_matching_configuration_fails_before_secrets(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    tenant = TenantId("tn_01M0VKA9S6KX7HRBG3G3ETYNB8")
    secrets = _TrackingEnvironmentSecrets()
    transport_calls = 0
    monkeypatch.setattr(
        runtime_module, "EnvironmentSecretResolver", lambda environ: secrets
    )

    def transport_factory() -> _NoIoHunterTransport:
        nonlocal transport_calls
        transport_calls += 1
        return _NoIoHunterTransport()

    factory = runtime_module.SchedulerRuntimeFactory(
        _factory_environ(db_url, tenant, hunter_enabled=True),
        _factory_dependencies(runtime_module, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
        hunter_transport_factory=transport_factory,
    )

    with pytest.raises(ValidationError, match="Hunter 当前配置未声明"):
        async with factory():
            pytest.fail("未声明当前配置不得完成 runtime 装配")
    assert secrets.calls == []
    assert transport_calls == 0


async def test_hunter_configured_pending_builds_fail_closed_adapters(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    tenant = TenantId("tn_01M0VKA9S6KX7HRBG3G3ETYNB9")
    await _seed_hunter_readiness(db_url, tenant, "configured")
    secrets = _TrackingEnvironmentSecrets()
    transport = _NoIoHunterTransport()
    captured: list[object] = []
    real_builder = runtime_module.build_hunter_contact_tools
    monkeypatch.setattr(
        runtime_module, "EnvironmentSecretResolver", lambda environ: secrets
    )

    def capturing_builder(**kwargs):
        tools = real_builder(**kwargs)
        captured.append(tools)
        return tools

    monkeypatch.setattr(runtime_module, "build_hunter_contact_tools", capturing_builder)
    factory = runtime_module.SchedulerRuntimeFactory(
        _factory_environ(db_url, tenant, hunter_enabled=True),
        _factory_dependencies(runtime_module, with_hunter=True),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
        hunter_transport_factory=lambda: transport,
    )

    async with factory() as runtime:
        assert runtime.activation is None
        assert "account_discovery.find_contacts" in runtime.workflow._handlers
        assert ("account_discovery", 2) in runtime.workflow._definitions
        assert {
            name
            for name, _handler in runtime.outbox._handlers["CampaignStateChanged"]
        } == {"account_discovery.campaign_state_changed"}
        assert "account_discovery.campaign_approval_decided" in {
            name for name, _handler in runtime.outbox._handlers["ApprovalDecided"]
        }
        assert len(captured) == 1
        assert captured[0].manifest_ids == ()
        assert captured[0].registered_configuration_hash is None
    assert "HUNTER_API_KEY_REF" not in secrets.calls
    assert transport.calls == 0


async def test_hunter_passed_without_account_discovery_fails_closed(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    tenant = TenantId("tn_01M0VKA9S6KX7HRBG3G3ETYNBA")
    await _seed_hunter_readiness(db_url, tenant, "passed")
    secrets = _TrackingEnvironmentSecrets()
    monkeypatch.setattr(
        runtime_module, "EnvironmentSecretResolver", lambda environ: secrets
    )
    factory = runtime_module.SchedulerRuntimeFactory(
        _factory_environ(db_url, tenant, hunter_enabled=True),
        _factory_dependencies(runtime_module, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
        hunter_transport_factory=_NoIoHunterTransport,
    )

    with pytest.raises(ValidationError, match="Hunter 生产组合不完整"):
        async with factory():
            pytest.fail("已验证配置缺 account discovery 不得启动")
    assert secrets.calls == []


@pytest.mark.parametrize("initial_state", ["passed", "ready"])
async def test_hunter_validated_configuration_builds_exact_tools_and_activation(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: str,
) -> None:
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    suffix = "C" if initial_state == "passed" else "D"
    tenant = TenantId(f"tn_01M0VKA9S6KX7HRBG3G3ETYNB{suffix}")
    configuration = await _seed_hunter_readiness(db_url, tenant, initial_state)
    secrets = _TrackingEnvironmentSecrets()
    transport = _NoIoHunterTransport()
    captured: list[object] = []
    real_builder = runtime_module.build_hunter_contact_tools
    monkeypatch.setattr(
        runtime_module, "EnvironmentSecretResolver", lambda environ: secrets
    )

    def capturing_builder(**kwargs):
        tools = real_builder(**kwargs)
        captured.append(tools)
        return tools

    monkeypatch.setattr(runtime_module, "build_hunter_contact_tools", capturing_builder)
    factory = runtime_module.SchedulerRuntimeFactory(
        _factory_environ(db_url, tenant, hunter_enabled=True),
        _factory_dependencies(runtime_module, with_hunter=True),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
        hunter_transport_factory=lambda: transport,
    )

    async with factory() as runtime:
        assert runtime.activation is not None
        assert len(captured) == 1
        assert captured[0].manifest_ids == ("contact.enrich", "contact.verify")
        assert (
            captured[0].registered_configuration_hash
            == configuration.configuration_hash
        )
        assert "HUNTER_API_KEY_REF" not in secrets.calls
        assert transport.calls == 0

        result = await _scheduler().run_scheduler_worker(
            runtime,
            wait=_stop_after_waits(1),
            install_signal_handlers=False,
        )
        assert result.status is _scheduler().WorkerStartStatus.STARTED

    assert transport.calls == 0
    events = await _hunter_readiness_events(db_url, tenant)
    runtime_events = [
        event for event in events if event.event_type.value == "runtime_composed"
    ]
    assert len(runtime_events) == 1
    assert runtime_events[0].actor_id == "system:scheduler-hunter"
    assert runtime_events[0].idempotency_key == (
        "hunter-runtime:config-v1:"
        f"{configuration.connector_profile_version}"
    )


async def test_production_factory_builds_complete_runtime_and_cleans_resources(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 schema 下装配 DNS/Gateway/workflow/outbox，退出关闭 health 与 engine。"""
    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")

    class Opportunity:
        async def record_handoff_escalation(self, *args, **kwargs):
            del args, kwargs

    class Employees:
        async def get_employee(self, *args, **kwargs):
            del args, kwargs

    class Audience:
        async def recipients_for(self, tenant_id, event):
            del tenant_id, event
            return ()

    class Resolver:
        async def resolve(self, name, rdtype):
            del name, rdtype
            pytest.fail("composition 不得触发真实 DNS")

    class HealthServer:
        def __init__(self, state, port):
            self.state = state
            self.port = port
            self.started = asyncio.Event()
            self.closed = asyncio.Event()

        async def serve(self):
            self.started.set()
            await self.closed.wait()

        async def wait_started(self):
            await self.started.wait()

        async def close(self):
            self.closed.set()

    servers: list[HealthServer] = []

    def health_factory(state, port):
        server = HealthServer(state, port)
        servers.append(server)
        return server

    disposed = 0
    original_dispose = AsyncEngine.dispose

    async def tracked_dispose(engine):
        nonlocal disposed
        disposed += 1
        await original_dispose(engine)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)
    environ = {
        "DATABASE_URL": db_url,
        "TRADEOS_TENANT_ID": "tn_01K2C5R6J7ABCDEFGHJKMNPQRS",
        "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
        "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
        "TRADEOS_SCHEDULER_LOCK_KEY": "3110001",
        "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "7",
        "TRADEOS_HANDOFF_T1_SECONDS": "3600",
        "TRADEOS_HANDOFF_T2_SECONDS": "7200",
        "TRADEOS_DKIM_SELECTOR": "s1",
        "TRADEOS_SCHEDULER_HEALTH_PORT": "8094",
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "SCHEDULER_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_REF",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "route-scheduler",
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsub.example",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "k1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": '{"k1": "UNSUBSCRIBE_HMAC_CURRENT"}',
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
        "SCHEDULER_FINGERPRINT_KEY": "x" * 32,
    }
    dependencies = runtime_module.SchedulerDomainDependencies(
        Opportunity(), Employees(), Audience()
    )
    factory = runtime_module.SchedulerRuntimeFactory(
        environ,
        dependencies,
        resolver_factory=Resolver,
        health_server_factory=health_factory,
        now=lambda: datetime(2026, 8, 14, 12, tzinfo=UTC),
    )
    async with factory() as runtime:
        assert runtime.outbox._max_attempts == 7
        assert set(runtime.workflow._handlers) == {
            "human_handoff.notify_owner",
            "human_handoff.accept",
            "human_handoff.escalate_manager",
            "human_handoff.escalate_boss",
            "human_handoff.remind_boss",
            "sending_identity_auth.check",
            "playbook_change.assemble",
            "playbook_change.submit",
            "playbook_change.wait",
            "playbook_change.expire",
            "playbook_change.apply",
            "playbook_change.mark_applied",
            "country_policy_change.assemble",
            "country_policy_change.submit",
            "country_policy_change.wait",
            "country_policy_change.expire",
            "country_policy_change.apply",
            "country_policy_change.mark_applied",
        }
        assert {
            definition.workflow_type
            for definition in runtime.workflow._definitions.values()
        } >= {
            "human_handoff",
            "sending_identity_authentication",
            "playbook_change",
            "country_policy_change",
        }
        assert set(runtime.outbox._handlers) == {
            "HandoffRequested",
            "HandoffAccepted",
            "HandoffQueueBacklogged",
            "SendingIdentitySuspended",
            "ReputationThresholdBreached",
            "CommitmentOverdue",
            "ApprovalDecided",
            "AuthenticationCheckRequested",
            "CountryPolicyVersionProposed",
        }
        assert {
            name for name, _handler in runtime.outbox._handlers["ApprovalDecided"]
        } == {
            "notification.approval_decided",
            "playbook_change.approval_decided",
            "country_policy_change.approval_decided",
        }
        assert {
            name
            for name, _handler in runtime.outbox._handlers[
                "CountryPolicyVersionProposed"
            ]
        } == {"country_policy_change.version_proposed"}
        assert (
            runtime.workflow._handlers["sending_identity_auth.check"]._selector == "s1"
        )
        assert servers[0].state.is_ready is True
    assert servers[0].closed.is_set()
    assert disposed == 1
