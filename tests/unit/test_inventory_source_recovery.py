"""合成来源中断恢复：外部派发次数及最终交付。"""

import asyncio

import pytest

from tests.unit.test_inventory_acquisition_driver import (
    NOW,
    Authority,
    Backend,
    Catalog,
    Collector,
    Sources,
    driver_type,
)


class RecoveryBackend(Backend):
    def __init__(self):
        super().__init__()
        self.progress = {}

    async def initialize_source_plan(self, tenant, job, token, plan):
        from datetime import timedelta

        self.job = self.job.model_copy(
            update={
                "source_plan": plan,
                "collection_deadline": NOW + timedelta(hours=24),
            }
        )
        return self.job

    async def source_progress(self, *args):
        return dict(self.progress)

    async def reserve_source(self, tenant, job, token, source):
        assert source not in self.progress
        self.progress[source] = "started"
        seconds = 480 // len(self.job.source_plan)
        self.job = self.job.model_copy(
            update={
                "collection_reserved_seconds": self.job.collection_reserved_seconds
                + seconds,
            }
        )
        return seconds

    async def record_source_attempt(self, *args, **kwargs):
        self.progress[args[3]] = args[4]
        await super().record_source_attempt(*args, **kwargs)


@pytest.mark.parametrize("crash", ["after_complete", "after_save", "unknown"])
async def test_resume_only_unstarted_sources_and_deliver(crash):
    backend = RecoveryBackend()
    collector = Collector()
    original = collector.collect
    save = backend.save_collected
    record = backend.record_source_attempt

    async def collect(*args, **kwargs):
        result = await original(*args, **kwargs)
        if crash == "unknown" and len(collector.sources) == 1:
            raise asyncio.CancelledError
        return result

    async def saving(*args):
        result = await save(*args)
        if crash == "after_save" and len(collector.sources) == 1:
            raise asyncio.CancelledError
        return result

    async def recording(*args, **kwargs):
        await record(*args, **kwargs)
        if crash == "after_complete" and len(collector.sources) == 1:
            raise asyncio.CancelledError

    collector.collect = collect
    backend.save_collected = saving
    backend.record_source_attempt = recording

    def driver():
        return driver_type()(
            tenant_id="tenant",
            backend=backend,
            catalog=Catalog(),
            authority=Authority(),
            sources=Sources(),
            collector=collector,
            now=lambda: NOW,
        )

    with pytest.raises(asyncio.CancelledError):
        await driver().scan()
    deadline = backend.job.collection_deadline
    backend.job = backend.job.model_copy(update={"attempts": 2, "lease_token": "new"})
    await driver().scan()
    assert len(collector.sources) == len(set(collector.sources)) == 2
    assert backend.calls[-1] == "deliver"
    assert backend.job.collection_reserved_seconds == 480
    assert backend.job.collection_deadline == deadline


async def test_recovery_reuses_plan_without_prepare_probe():
    backend, collector = RecoveryBackend(), Collector()

    class ProbedSources(Sources):
        preparations = 0

        async def prepare(self, *args):
            self.preparations += 1

    sources = ProbedSources()

    async def crash(*args, **kwargs):
        collector.sources.append(args[1])
        raise asyncio.CancelledError

    original = collector.collect
    collector.collect = crash

    def driver():
        return driver_type()(
            tenant_id="tenant",
            backend=backend,
            catalog=Catalog(),
            authority=Authority(),
            sources=sources,
            collector=collector,
            now=lambda: NOW,
        )

    with pytest.raises(asyncio.CancelledError):
        await driver().scan()
    collector.collect = original
    backend.job = backend.job.model_copy(update={"attempts": 2, "lease_token": "new"})
    await driver().scan()
    assert sources.preparations == 1
    assert len(collector.sources) == len(set(collector.sources)) == 2


async def test_current_source_revocation_blocks_remaining_external_io():
    backend, collector = RecoveryBackend(), Collector()
    await backend.initialize_source_plan(
        "tenant", backend.job.id, "lease", ("company_websites",)
    )

    class Revoked(Sources):
        async def list_sources(self, *args):
            return ()

    await driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Catalog(),
        authority=Authority(),
        sources=Revoked(),
        collector=collector,
        now=lambda: NOW,
    ).scan()
    assert collector.sources == []
    assert backend.calls[-1] == "collector_failed"
