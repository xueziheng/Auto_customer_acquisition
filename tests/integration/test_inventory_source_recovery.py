"""真实PG来源边界恢复；仅合成公开候选与适配模型。"""

import asyncio
from datetime import timedelta

import pytest

from apps.scheduler_worker.inventory_acquisition import InventoryAcquisitionDriver
from domains.prospecting.inventory import InventoryQuery
from domains.prospecting.inventory_fit import (
    InventoryFitAssessment,
    candidate_evidence_version,
)
from infra.db.prospect_inventory import PostgresInventoryService
from shared.schemas.identifiers import EmployeeId
from tests.integration.test_prospect_inventory import NOW
from tests.integration.test_prospect_inventory import inventory as inventory_fixture

inventory = inventory_fixture
from domains.prospecting.collection_result import CollectionResult
from domains.prospecting.projection_diagnostic import ProjectionSummary
from tests.unit.test_inventory_acquisition_driver import Authority, Collector, Sources
from tests.unit.test_prospect_inventory import candidate


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("crash", ["complete", "saved", "unknown"])
async def test_real_restart_resumes_remaining_source_without_duplicate(
    inventory, private, crash
):
    service, catalog, factory, tenant, _ = inventory
    clock = [NOW]
    service.now = lambda: clock[0]
    query = InventoryQuery(
        product_tags=("hinges",), intent="合成经销商" if private else None
    )
    await service.request(tenant, EmployeeId("emp"), query, 1, "resume")
    calls = []
    original_save = service.save_collected
    original_record = service.record_source_attempt
    original_claim = service.claim_job
    leased = []

    async def claim(*args):
        job = await original_claim(*args)
        leased.append(job)
        return job

    service.claim_job = claim

    class SyntheticCollector(Collector):
        async def collect(self, job, source, guard, **kwargs):
            await guard()
            calls.append(source)
            if crash == "unknown" and len(calls) == 1:
                raise asyncio.CancelledError
            c = candidate(
                id="pci_" + source,
                canonical_domain=source.replace("_", "") + ".example",
                public_emails=(),
            )
            return CollectionResult(
                (c,),
                ProjectionSummary(
                    input_shape="valid",
                    raw_count=1,
                    projected_count=1,
                    projection_complete=True,
                ),
            )

    async def saving(*args):
        result = await original_save(*args)
        if crash == "saved" and len(calls) == 1:
            raise asyncio.CancelledError
        return result

    async def recording(*args, **kwargs):
        await original_record(*args, **kwargs)
        if crash == "complete" and len(calls) == 1:
            raise asyncio.CancelledError

    service.save_collected = saving
    service.record_source_attempt = recording

    class Fit:
        async def evaluate(self, job, batch, candidates, guard):
            await guard()
            return tuple(
                InventoryFitAssessment(
                    candidate_id=c.id,
                    evidence_version=candidate_evidence_version(c),
                    matched=True,
                    evidence_quote=c.evidence_text,
                    inference="合成推断",
                    extracted_by="synthetic",
                    extracted_at=clock[0],
                )
                for c in candidates
            )

    collector = SyntheticCollector()

    def driver(backend):
        return InventoryAcquisitionDriver(
            tenant_id=tenant,
            backend=backend,
            catalog=catalog,
            authority=Authority(),
            sources=Sources(),
            collector=collector,
            fit_model=Fit(),
            now=lambda: clock[0],
        )

    with pytest.raises(asyncio.CancelledError):
        await driver(service).scan()
    first = await service.get_job(tenant, leased[0].id)
    assert first.collection_reserved_seconds == 240
    clock[0] += timedelta(minutes=11)
    restarted = PostgresInventoryService(factory, tenant, catalog, now=lambda: clock[0])
    await driver(restarted).scan()
    current = await restarted.get_job(tenant, first.id)
    assert current.status == "complete"
    assert current.collection_deadline == first.collection_deadline
    assert current.collection_reserved_seconds <= 480
    assert len(calls) == len(set(calls))
    assert len(await restarted.list_allocations(tenant, None, 10)) == 1
    with pytest.raises(ValueError):
        await restarted.reserve_source(
            tenant, first.id, first.lease_token, first.source_plan[-1]
        )
    with pytest.raises(ValueError):
        await restarted.save_collected(tenant, first.id, first.lease_token, ())


async def test_persistent_budget_unknown_no_remaining_and_expiry(inventory):
    service, catalog, factory, tenant, _ = inventory
    await service.request(
        tenant, EmployeeId("emp"), InventoryQuery(product_tags=("hinges",)), 1, "budget"
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    plan = ("company_websites", "industry_directories", "company_news")
    initial = await service.initialize_source_plan(
        tenant, job.id, job.lease_token, plan
    )
    for source in plan:
        assert (
            await service.reserve_source(tenant, job.id, job.lease_token, source) == 160
        )
    restarted = PostgresInventoryService(
        factory, tenant, catalog, now=lambda: NOW + timedelta(minutes=11)
    )
    resumed = await restarted.claim_job(tenant, timedelta(minutes=10))
    assert resumed.collection_reserved_seconds == 480
    assert resumed.collection_deadline == initial.collection_deadline
    for source in plan:
        with pytest.raises(ValueError):
            await restarted.reserve_source(tenant, job.id, resumed.lease_token, source)
    calls = Collector()
    recovery = PostgresInventoryService(
        factory, tenant, catalog, now=lambda: NOW + timedelta(minutes=22)
    )
    await InventoryAcquisitionDriver(
        tenant_id=tenant,
        backend=recovery,
        catalog=catalog,
        authority=Authority(),
        sources=Sources(),
        collector=calls,
        now=lambda: NOW + timedelta(minutes=22),
    ).scan()
    assert calls.sources == []
    assert (await recovery.get_job(tenant, job.id)).failure_reason == "collector_failed"

    # 新任务固定期限24h，跨租约不延长。
    await service.request(
        tenant, EmployeeId("emp"), InventoryQuery(product_tags=("hinges",)), 1, "expiry"
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    await service.initialize_source_plan(tenant, job.id, job.lease_token, plan)
    expired = PostgresInventoryService(
        factory, tenant, catalog, now=lambda: NOW + timedelta(hours=25)
    )
    await InventoryAcquisitionDriver(
        tenant_id=tenant,
        backend=expired,
        catalog=catalog,
        authority=Authority(),
        sources=Sources(),
        collector=calls,
        now=lambda: NOW + timedelta(hours=25),
    ).scan()
    assert calls.sources == []
    assert (await expired.get_job(tenant, job.id)).failure_reason == "budget_exhausted"


async def test_exhausted_plan_delivers_available_then_terminates_partial_job(inventory):
    service, catalog, _, tenant, _ = inventory
    await service.request(
        tenant,
        EmployeeId("emp"),
        InventoryQuery(product_tags=("hinges",)),
        2,
        "partial",
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    await service.initialize_source_plan(
        tenant, job.id, job.lease_token, ("company_websites",)
    )
    await service.reserve_source(tenant, job.id, job.lease_token, "company_websites")
    await catalog.save_public((candidate(),))
    result = await service.deliver_job(tenant, job.id, job.lease_token)
    assert len(result.allocations) == 1
    assert result.job.status == "failed"
    assert result.job.failure_reason == "budget_exhausted"
    assert await service.claim_job(tenant, timedelta(minutes=10)) is None


async def test_reclaimed_source_ledger_rejects_old_and_other_tenant_writers(inventory):
    from shared.schemas.identifiers import TenantId

    service, catalog, factory, tenant, _ = inventory
    await service.request(
        tenant, EmployeeId("emp"), InventoryQuery(product_tags=("hinges",)), 1, "fence"
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    await service.initialize_source_plan(
        tenant, job.id, job.lease_token, ("company_websites", "company_news")
    )
    await service.reserve_source(tenant, job.id, job.lease_token, "company_websites")
    resumed = PostgresInventoryService(
        factory, tenant, catalog, now=lambda: NOW + timedelta(minutes=11)
    )
    current = await resumed.claim_job(tenant, timedelta(minutes=10))
    for token, bound_tenant in (
        (job.lease_token, tenant),
        (current.lease_token, TenantId("other")),
    ):
        with pytest.raises(ValueError):
            await resumed.reserve_source(bound_tenant, job.id, token, "company_news")
        with pytest.raises(ValueError):
            await resumed.record_source_attempt(
                bound_tenant, job.id, token, "company_websites", "complete", 0
            )
    assert await resumed.source_progress(tenant, job.id, current.lease_token) == {
        "company_websites": "started"
    }
    assert (
        await resumed.reserve_source(
            tenant, job.id, current.lease_token, "company_news"
        )
        == 240
    )
