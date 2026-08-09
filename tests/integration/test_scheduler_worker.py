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
) -> Any:
    return module.SchedulerRuntime(
        lock_engine=lock_engine,
        outbox=drainer,
        workflow=poller,
        tenant_id=tenant,
        config=_config(module, lock_key=lock_key),
    )


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
