"""真实 PG 验证显式人工对账终态；原默认恢复路径保持原语义。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.repositories.tool_calls import ToolCallRepositoryImpl
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_tool_call_repository import (
    engine_fx as engine_fx,  # noqa: PLC0414
)
from tests.integration.test_tool_gateway_pipeline import (
    _checks,
    _Clock,
    _context,
    _Handler,
    _manifest,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolCallResult, ToolGateway
from tool_gateway.repository import ToolCallId


class Handler(_Handler):
    def __init__(self, mode):
        super().__init__()
        self.mode, self.started = mode, asyncio.Event()

    async def execute(self, tenant, prepared):
        self.execute_calls += 1
        self.started.set()
        if self.mode == "cancel":
            await asyncio.Event().wait()
        if self.mode == "unexpected":
            raise RuntimeError("PRIVATE_PROVIDER_ERROR")
        categories = {
            "permanent": ToolErrorCategory.PROVIDER_PERMANENT,
            "transient": ToolErrorCategory.PROVIDER_TRANSIENT,
            "unknown": ToolErrorCategory.RECONCILIATION_REQUIRED,
        }
        if self.mode in categories:
            raise ToolGatewayError(categories[self.mode])
        return {"provider_ref": "fixture_ref_1", "already_existed": False}


def gateway(factory, clock, handler, manual):
    registry = ToolRegistry()
    registry.register(_manifest(), handler)
    checks, _ = _checks()
    return ToolGateway(
        registry,
        checks,
        factory,
        lease_duration=timedelta(seconds=5),
        lease_owner="v6-test",
        now=clock.now,
        id_factory=new_id,
        manual_reconciliation_only=manual,
    )


def setup(engine):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = _Clock(datetime.now(UTC))
    tenant = TenantId(new_id("tn"))
    factory = lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t, now=clock.now)
    return sessions, clock, tenant, factory


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize(
    "mode", ["success", "permanent", "transient", "unknown", "unexpected"]
)
async def test_policy_keeps_success_and_known_failure_but_closes_unknown(
    engine_fx, manual, mode
):
    _, clock, tenant, factory = setup(engine_fx)
    handler = Handler(mode)
    client = gateway(factory, clock, handler, manual)
    ctx = _context(tenant, new_id("key"))
    result = await client.invoke(ctx)
    unknown = mode == "unknown" or (manual and mode == "unexpected")
    expected = (
        ToolCallStatus.SUCCEEDED
        if mode == "success"
        else (
            ToolCallStatus.FAILED_TRANSIENT
            if mode == "transient" or (unknown and not manual)
            else ToolCallStatus.FAILED_PERMANENT
        )
    )
    assert result.status is expected
    if unknown:
        assert result.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
    async with factory(tenant) as uow:
        row = await uow.calls.get(tenant, ToolCallId(result.tool_call_id))
    assert row.status is expected and row.attempt_count == 1
    if manual and unknown:
        assert (
            row.lease_owner is None
            and row.retry_after_at is None
            and row.completed_at is not None
        )
        clock.value += timedelta(days=2)
        for candidate in (client, gateway(factory, clock, handler, manual)):
            repeated = await candidate.invoke(ctx)
            assert repeated.status is ToolCallStatus.FAILED_PERMANENT
            assert repeated.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
            assert repeated.tool_call_id == result.tool_call_id
        async with factory(tenant) as uow:
            assert await uow.calls.get(tenant, ToolCallId(result.tool_call_id)) == row
    assert handler.execute_calls == 1


@pytest.mark.parametrize("manual", [False, True])
async def test_cancel_propagates_and_expired_key_never_executes_again(
    engine_fx, manual
):
    _, clock, tenant, factory = setup(engine_fx)
    handler = Handler("cancel")
    ctx = _context(tenant, new_id("key"))
    client = gateway(factory, clock, handler, manual)
    task = asyncio.create_task(client.invoke(ctx))
    await asyncio.wait_for(handler.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    clock.value += timedelta(days=2)
    repeated = await gateway(factory, clock, handler, manual).invoke(ctx)
    assert repeated.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert repeated.status is (
        ToolCallStatus.FAILED_PERMANENT if manual else ToolCallStatus.FAILED_TRANSIENT
    )
    async with factory(tenant) as uow:
        row = await uow.calls.get(tenant, ToolCallId(repeated.tool_call_id))
    assert row.status is (
        ToolCallStatus.FAILED_PERMANENT if manual else ToolCallStatus.EXECUTING
    )
    assert row.attempt_count == 1 and handler.execute_calls == 1


async def test_second_cancel_waits_for_owned_cleanup_and_keeps_primary(
    engine_fx, monkeypatch
):
    _, clock, tenant, factory = setup(engine_fx)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ToolCallRepositoryImpl.append_event

    async def append(self, event):
        if event.rule == "cancellation:manual_reconciliation":
            entered.set()
            await release.wait()
        await original(self, event)

    monkeypatch.setattr(ToolCallRepositoryImpl, "append_event", append)
    handler = Handler("cancel")
    ctx = _context(tenant, new_id("key"))
    task = asyncio.create_task(gateway(factory, clock, handler, True).invoke(ctx))
    try:
        await asyncio.wait_for(handler.started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    repeated = await gateway(factory, clock, handler, True).invoke(ctx)
    assert repeated.status is ToolCallStatus.FAILED_PERMANENT
    assert repeated.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert handler.execute_calls == 1


async def test_cancel_after_success_commit_preserves_committed_evidence(engine_fx):
    sessions, clock, tenant, _ = setup(engine_fx)
    committed = asyncio.Event()

    class Uow(SqlAlchemyToolGatewayUnitOfWork):
        async def __aenter__(self):
            await super().__aenter__()
            self.success = False
            original = self.calls.append_event

            async def append(event):
                self.success = self.success or event.outcome == "succeeded"
                await original(event)

            self.calls.append_event = append
            return self

        async def __aexit__(self, exc_type, exc, tb):
            await super().__aexit__(exc_type, exc, tb)
            if exc_type is None and self.success:
                committed.set()
                await asyncio.Event().wait()

    factory = lambda t: Uow(sessions, t, now=clock.now)
    handler = Handler("success")
    ctx = _context(tenant, new_id("key"))
    task = asyncio.create_task(gateway(factory, clock, handler, True).invoke(ctx))
    await asyncio.wait_for(committed.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    repeated = await gateway(factory, clock, handler, True).invoke(ctx)
    assert repeated.status is ToolCallStatus.DUPLICATE
    assert repeated.output["provider_ref"] == "fixture_ref_1"
    assert handler.execute_calls == 1


def test_terminal_reconciliation_is_valid_but_cannot_advertise_retry():
    result = ToolCallResult(
        "model.generate",
        ToolCallStatus.FAILED_PERMANENT,
        error_category=ToolErrorCategory.RECONCILIATION_REQUIRED,
    )
    assert result.retry_after_seconds is None
    with pytest.raises(ValidationError):
        ToolCallResult(
            "model.generate",
            ToolCallStatus.FAILED_PERMANENT,
            error_category=ToolErrorCategory.RECONCILIATION_REQUIRED,
            retry_after_seconds=1,
        )
