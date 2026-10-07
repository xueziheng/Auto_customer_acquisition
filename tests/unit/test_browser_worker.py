from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from apps.browser_worker.main import (
    BrowserJob,
    BrowserSnapshot,
    BrowserWorkerConfig,
    BrowserWorkerRuntime,
    WorkerReadinessError,
    run_browser_worker,
)
from shared.schemas.identifiers import RunId, TenantId


@dataclass
class Jobs:
    queued: list[BrowserJob] = field(default_factory=list)
    claim_limits: list[int] = field(default_factory=list)
    completed: list[tuple[str, BrowserSnapshot]] = field(default_factory=list)
    failed: list[tuple[str, str, bool]] = field(default_factory=list)

    async def assert_schema_current(self) -> None:
        return None

    async def claim_gateway_reads(self, *, limit: int) -> tuple[BrowserJob, ...]:
        self.claim_limits.append(limit)
        claimed = tuple(self.queued[:limit])
        del self.queued[:limit]
        return claimed

    async def complete(self, *, job_id: str, snapshot: BrowserSnapshot) -> None:
        self.completed.append((job_id, snapshot))

    async def fail(
        self, *, job_id: str, category: str, retryable: bool
    ) -> None:
        self.failed.append((job_id, category, retryable))


class Reader:
    def __init__(self, stop: asyncio.Event | None = None) -> None:
        self.stop = stop
        self.calls: list[BrowserJob] = []

    async def assert_ready(self) -> None:
        return None

    async def read_public_page(self, job: BrowserJob) -> BrowserSnapshot:
        self.calls.append(job)
        if self.stop is not None:
            self.stop.set()
        return BrowserSnapshot(
            canonical_url="https://example.com/catalog",
            observed_at="2026-08-21T10:00:00Z",
            content_hash="a" * 64,
            snapshot_artifact_ref="art_01K39P9M5D6K4A91YEQ80EJZ0X",
        )


def job(*, authorized: bool = True, suffix: str = "one") -> BrowserJob:
    return BrowserJob(
        job_id=f"browser-job-{suffix}",
        tenant_id=TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X"),
        run_id=RunId("run_01K39P9M5D6K4A91YEQ80EJZ0X"),
        tool_call_id="tool-call-1",
        url="https://example.com/catalog",
        gateway_authorized=authorized,
    )


@pytest.mark.asyncio
async def test_browser_worker_rejects_non_gateway_job_without_page_read() -> None:
    stop = asyncio.Event()
    jobs = Jobs(queued=[job(authorized=False)])
    reader = Reader(stop)
    runtime = BrowserWorkerRuntime(
        jobs=jobs,
        reader=reader,
        config=BrowserWorkerConfig(batch_limit=1, idle_seconds=1),
    )

    async def stop_after_failure(_seconds: float, stop_event: asyncio.Event) -> None:
        stop_event.set()

    result = await run_browser_worker(
        runtime,
        stop_event=stop,
        wait=stop_after_failure,
        install_signal_handlers=False,
    )

    assert result.jobs_failed == 1
    assert reader.calls == []
    assert jobs.failed == [("browser-job-one", "gateway_authorization", False)]


@pytest.mark.asyncio
async def test_browser_worker_finishes_current_snapshot_then_stops() -> None:
    stop = asyncio.Event()
    jobs = Jobs(queued=[job(suffix="one"), job(suffix="two")])
    reader = Reader(stop)
    runtime = BrowserWorkerRuntime(
        jobs=jobs,
        reader=reader,
        config=BrowserWorkerConfig(batch_limit=2, idle_seconds=1),
    )

    result = await run_browser_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.jobs_completed == 1
    assert jobs.claim_limits == [2]
    assert len(reader.calls) == 1
    assert jobs.completed[0][1].snapshot_artifact_ref.startswith("art_")


@pytest.mark.asyncio
async def test_browser_worker_does_not_become_ready_when_reader_is_unavailable() -> None:
    ready = asyncio.Event()

    class UnavailableReader(Reader):
        async def assert_ready(self) -> None:
            raise WorkerReadinessError("browser_registry_unavailable")

    runtime = BrowserWorkerRuntime(
        jobs=Jobs(),
        reader=UnavailableReader(),
        config=BrowserWorkerConfig(batch_limit=1, idle_seconds=1),
    )

    with pytest.raises(WorkerReadinessError, match="browser_registry_unavailable"):
        await run_browser_worker(
            runtime,
            ready_event=ready,
            install_signal_handlers=False,
        )

    assert not ready.is_set()

