"""固定工具槽统计必须区分排队、在途、终态与无预留拒绝。"""

import asyncio

import pytest

from connectors.codex_agent.discovery import DiscoverySession, DiscoveryToolFailure


def checked(session):
    from domains.prospecting.projection_diagnostic import ToolUsageSummary

    return ToolUsageSummary.model_validate(session.usage_snapshot())


async def test_queue_inflight_and_cancellation_conserve_reserved_slots():
    entered = asyncio.Event()

    async def execute(name, arguments):
        entered.set()
        await asyncio.Event().wait()

    async with DiscoverySession(
        execute, max_requests=7, timeout_seconds=5, max_result_bytes=1000
    ) as session:
        first = asyncio.create_task(session.call("source_search", {"query": "PRIVATE"}))
        await entered.wait()
        second = asyncio.create_task(
            session.call("read_public_page", {"page_id": "PRIVATE"})
        )
        await asyncio.sleep(0)
        usage = checked(session)
        assert (
            usage.reserved,
            usage.queued,
            usage.inflight,
            usage.terminal,
            usage.cancelled,
        ) == (2, 1, 1, 0, 0)
        await session.close()
        await asyncio.gather(first, second, return_exceptions=True)
        usage = checked(session)
        assert (usage.queued, usage.inflight, usage.cancelled) == (0, 0, 2)
        assert "PRIVATE" not in usage.model_dump_json()


async def test_terminal_rejections_and_unreserved_limits_are_separate_and_bounded():
    async def execute(name, arguments):
        if name == "source_search":
            raise DiscoveryToolFailure("search_limit_reached")
        return {}

    async with DiscoverySession(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=1000
    ) as session:
        await session.call("source_search", {})
        await session.call("read_public_page", {})
        for _ in range(105):
            await session.call("browse_public_page", {})
        usage = checked(session)
        assert (
            usage.reserved,
            usage.terminal,
            usage.succeeded,
            usage.search_limit_reached,
        ) == (2, 2, 1, 1)
        assert usage.unreserved_budget == 100 and usage.unreserved_overflow
        assert usage.browse_public_page == 0
    async with DiscoverySession(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=1000
    ) as session:
        session._deadline = 0
        await session.call("source_search", {})
        usage = checked(session)
        assert usage.reserved == 0 and usage.unreserved_deadline == 1


@pytest.mark.parametrize("error", [PermissionError, TimeoutError])
async def test_closed_failure_accounts_for_queued_calls_without_dispatch(error):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(name, arguments):
        calls.append(name)
        entered.set()
        await release.wait()
        raise error("PRIVATE")

    async with DiscoverySession(
        execute, max_requests=7, timeout_seconds=5, max_result_bytes=1000
    ) as session:
        first = asyncio.create_task(session.call("source_search", {}))
        await entered.wait()
        second = asyncio.create_task(session.call("read_public_page", {}))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(v, ValueError) for v in results)
        usage = checked(session)
        assert usage.terminal == usage.failed_closed == usage.reserved == 2
        assert "PRIVATE" not in usage.model_dump_json()
    assert calls == ["source_search"]


def test_domain_summary_unknown_legacy_and_strict_conservation():
    from domains.prospecting.projection_diagnostic import (
        ProjectionSummary,
        ToolUsageSummary,
    )

    assert ProjectionSummary(input_shape="valid", raw_count=0).tool_usage is None
    valid = {
        "tool_limit": 7,
        "reserved": 1,
        "source_search": 1,
        "terminal": 1,
        "succeeded": 1,
    }
    ToolUsageSummary.model_validate(valid)
    for update in (
        {"reserved": True},
        {"reserved": 101},
        {"terminal": 0},
        {"query": "PRIVATE"},
        {"inflight": 2},
        {"source_search": 0},
        {"unreserved_overflow": True},
        {"tool_limit": 0},
    ):
        with pytest.raises(ValueError):
            ToolUsageSummary.model_validate({**valid, **update})


@pytest.mark.parametrize("shape_failure", [False, True])
async def test_collector_carries_usage_for_empty_and_existing_shape_failure(
    shape_failure,
):
    from types import SimpleNamespace

    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from domains.prospecting.projection_diagnostic import (
        ProjectionDiagnosticError,
        ProjectionSummary,
    )
    from tests.unit.test_inventory_acquisition_driver import JOB

    async def search(*args, **kwargs):
        return SimpleNamespace(results=())

    async def factory(*args):
        return SimpleNamespace(searcher=SimpleNamespace(search=search))

    async def generate(job, source, session, payload):
        await session.call("source_search", {"source_id": source, "query": "PRIVATE"})
        await session.call("read_public_page", {"page_id": "PRIVATE"})
        if shape_failure:
            raise ProjectionDiagnosticError(ProjectionSummary(input_shape="not_object"))
        return {"candidates": []}

    async def guard():
        pass

    collector = CodexInventoryCollector(factory, generate)
    if shape_failure:
        with pytest.raises(ProjectionDiagnosticError) as exc:
            await collector.collect(JOB, "company_websites", guard)
        summary = exc.value.summary
        assert summary.input_shape == "not_object"
    else:
        summary = (await collector.collect(JOB, "company_websites", guard)).summary
        assert summary.raw_count == 0
    assert summary.tool_usage.reserved == summary.tool_usage.terminal == 2
    assert summary.tool_usage.succeeded == summary.tool_usage.page_id_unknown == 1
    assert "PRIVATE" not in summary.model_dump_json()


async def test_pending_calls_snapshot_is_not_claimed_terminal_on_shape_failure():
    entered = asyncio.Event()

    async def execute(name, arguments):
        entered.set()
        await asyncio.Event().wait()

    async with DiscoverySession(
        execute, max_requests=7, timeout_seconds=5, max_result_bytes=1000
    ) as session:
        pending = asyncio.create_task(session.call("read_public_page", {}))
        await entered.wait()
        snapshot = checked(session)
        assert snapshot.inflight == 1 and snapshot.terminal == 0
    await asyncio.gather(pending, return_exceptions=True)
    assert snapshot.inflight == 1 and checked(session).cancelled == 1
