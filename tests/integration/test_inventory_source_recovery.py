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


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("fresh", [False, True])
async def test_expired_recovery_delivers_only_current_saved_results_without_new_io(
    inventory, private, fresh
):
    service, catalog, factory, tenant, _ = inventory
    query = InventoryQuery(
        product_tags=("hinges",), intent="合成目标" if private else None
    )
    await service.request(tenant, EmployeeId("emp"), query, 2, "expired-results")
    job = await service.claim_job(tenant, timedelta(minutes=10))
    plan = ("company_websites", "industry_directories", "company_news")
    await service.initialize_source_plan(tenant, job.id, job.lease_token, plan)
    await service.reserve_source(tenant, job.id, job.lease_token, plan[0])
    saved = candidate(
        expires_at=NOW + timedelta(days=30) if fresh else NOW + timedelta(hours=1)
    )
    await service.save_collected(tenant, job.id, job.lease_token, (saved,))
    if private:
        batch = await service.reserve_fit_batch(
            tenant, job.id, job.lease_token, (saved,)
        )
        await service.complete_fit_batch(
            tenant,
            job.id,
            job.lease_token,
            batch.sequence,
            (
                InventoryFitAssessment(
                    candidate_id=saved.id,
                    evidence_version=candidate_evidence_version(saved),
                    matched=True,
                    evidence_quote=saved.evidence_text,
                    inference="合成推断",
                    extracted_by="synthetic",
                    extracted_at=NOW,
                ),
            ),
        )
    # 新证据无适配结果，到期之后也不得为它补模型调用。
    await catalog.save_public(
        (
            candidate(
                id="pci_unassessed",
                canonical_domain="unassessed.example",
                product_tags=("hinges",) if private else ("other",),
            ),
        )
    )
    io = []

    class NoIO(Sources):
        async def prepare(self, *args):
            io.append("prepare")

        async def list_sources(self, *args):
            io.append("sources")
            return await super().list_sources(*args)

        async def collect(self, *args, **kwargs):
            io.append("collect")
            raise AssertionError("期限已到")

        async def evaluate(self, *args):
            io.append("fit")
            raise AssertionError("期限已到")

    now = NOW + timedelta(hours=25)
    resumed = PostgresInventoryService(factory, tenant, catalog, now=lambda: now)
    ports = NoIO()
    await InventoryAcquisitionDriver(
        tenant_id=tenant,
        backend=resumed,
        catalog=catalog,
        authority=Authority(),
        sources=ports,
        collector=ports,
        fit_model=ports,
        now=lambda: now,
    ).scan()
    allocations = await resumed.list_allocations(tenant, None, 10)
    assert len(allocations) == int(fresh)
    current = await resumed.get_job(tenant, job.id)
    assert current.status == "failed" and current.failure_reason == "budget_exhausted"
    assert current.remaining_count == 2 - int(fresh)
    assert io == []
    assert await resumed.claim_job(tenant, timedelta(minutes=10)) is None
    with pytest.raises(ValueError):
        await resumed.deliver_job(tenant, job.id, job.lease_token)


@pytest.mark.parametrize("revoked", ["employee", "refs"])
async def test_expired_recovery_rechecks_authority_and_refs_before_delivery(
    inventory, revoked
):
    from shared.errors import PermissionDenied

    service, catalog, factory, tenant, _ = inventory
    query = InventoryQuery(
        product_tags=("hinges",),
        confirmed_refs=({"document_id": "doc", "revision_id": "rev"},),
    )
    await service.request(tenant, EmployeeId("emp"), query, 1, "expired-revoked")
    job = await service.claim_job(tenant, timedelta(minutes=10))
    await service.initialize_source_plan(
        tenant, job.id, job.lease_token, ("company_websites",)
    )
    saved = candidate()
    await catalog.save_public((saved,))
    batch = await service.reserve_fit_batch(tenant, job.id, job.lease_token, (saved,))
    await service.complete_fit_batch(
        tenant,
        job.id,
        job.lease_token,
        batch.sequence,
        (
            InventoryFitAssessment(
                candidate_id=saved.id,
                evidence_version=candidate_evidence_version(saved),
                matched=True,
                evidence_quote=saved.evidence_text,
                inference="合成推断",
                extracted_by="synthetic",
                extracted_at=NOW,
            ),
        ),
    )

    class CurrentAuthority(Authority):
        async def current_scope(self, *args):
            if revoked == "employee":
                raise PermissionDenied("员工已撤权")
            return await super().current_scope(*args)

    class Context:
        async def read(self, *args):
            if revoked == "refs":
                raise PermissionDenied("资料已撤权")

    now = NOW + timedelta(hours=25)
    resumed = PostgresInventoryService(factory, tenant, catalog, now=lambda: now)
    collector = Collector()
    await InventoryAcquisitionDriver(
        tenant_id=tenant,
        backend=resumed,
        catalog=catalog,
        authority=CurrentAuthority(),
        sources=Sources(),
        collector=collector,
        context_reader=Context(),
        now=lambda: now,
    ).scan()
    assert await resumed.list_allocations(tenant, None, 10) == ()
    assert (await resumed.get_job(tenant, job.id)).status == "failed"
    assert collector.sources == []


async def test_three_source_private_recovery_with_enough_fit_still_runs_final_exploration(
    inventory,
):
    service, catalog, factory, tenant, _ = inventory
    await service.request(
        tenant,
        EmployeeId("emp"),
        InventoryQuery(product_tags=("hinges",), intent="合成目标"),
        1,
        "exploration",
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    plan = ("company_websites", "industry_directories", "company_news")
    await service.initialize_source_plan(tenant, job.id, job.lease_token, plan)
    await service.reserve_source(tenant, job.id, job.lease_token, plan[0])
    saved = candidate()
    await service.save_collected(tenant, job.id, job.lease_token, (saved,))
    await service.record_source_attempt(
        tenant, job.id, job.lease_token, plan[0], "complete", 1
    )
    batch = await service.reserve_fit_batch(tenant, job.id, job.lease_token, (saved,))
    await service.complete_fit_batch(
        tenant,
        job.id,
        job.lease_token,
        batch.sequence,
        (
            InventoryFitAssessment(
                candidate_id=saved.id,
                evidence_version=candidate_evidence_version(saved),
                matched=True,
                evidence_quote=saved.evidence_text,
                inference="合成推断",
                extracted_by="synthetic",
                extracted_at=NOW,
            ),
        ),
    )

    class Exploration:
        def __init__(self):
            self.calls = []

        async def collect(self, job, source, guard, **kwargs):
            await guard()
            self.calls.append(source)
            return CollectionResult(
                (),
                ProjectionSummary(
                    input_shape="valid",
                    raw_count=0,
                    projected_count=0,
                    projection_complete=True,
                ),
            )

        async def evaluate(self, *args):
            pytest.fail("已完成且足量的fit不得再调用模型")

    class ThreeSources(Sources):
        async def prepare(self, *args):
            pytest.fail("恢复不得再次prepare")

        async def list_sources(self, *args):
            from domains.prospecting.inventory import SourceState

            return tuple(
                SourceState(
                    source_id=s,
                    availability="available",
                    cost_class="free",
                    relevant=True,
                )
                for s in plan
            )

    now = NOW + timedelta(minutes=11)
    resumed = PostgresInventoryService(factory, tenant, catalog, now=lambda: now)
    ports = Exploration()
    await InventoryAcquisitionDriver(
        tenant_id=tenant,
        backend=resumed,
        catalog=catalog,
        authority=Authority(),
        sources=ThreeSources(),
        collector=ports,
        fit_model=ports,
        now=lambda: now,
    ).scan()
    assert ports.calls == [plan[-1]]
    current = await resumed.get_job(tenant, job.id)
    assert current.status == "complete" and current.source_plan == plan
    assert current.collection_reserved_seconds == 320
    assert len(await resumed.list_allocations(tenant, None, 10)) == 1


@pytest.mark.parametrize("crash", ["reserved", "unknown"])
@pytest.mark.parametrize("transition", ["same", "downgrade", "upgrade"])
async def test_service_budget_checkpoint_crash_recovers_exact_reservation(inventory, crash, transition):
    from sqlalchemy import select

    from infra.db.inventory_service_plan import InventoryServicePlanBudget
    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore
    from infra.db.prospecting_service_tables import ProspectingServiceReservationRow

    service, catalog, factory, tenant, _ = inventory
    clock = [NOW]
    service.now = lambda: clock[0]
    store = PostgresProspectingServicePlanStore(factory)
    tier = "advanced" if transition == "downgrade" else "basic"
    count = 30 if tier == "advanced" else 10
    state = await store.assign(tenant, tier, expected_version=None, actor_id="operator", now=NOW)
    await service.request(tenant, EmployeeId("emp"), InventoryQuery(product_tags=("hinges",)), count, "source-crash")
    original = service.reserve_source
    crashed = []
    calls = []

    async def reserve(*args):
        if crash == "reserved" and not crashed:
            crashed.append(args[1])
            raise asyncio.CancelledError
        return await original(*args)

    service.reserve_source = reserve

    class EmptyCollector:
        async def collect(self, job, source, guard, *, target_count):
            await guard()
            calls.append((source, target_count))
            if crash == "unknown" and not crashed:
                crashed.append(job.id)
                raise asyncio.CancelledError
            return CollectionResult((), ProjectionSummary(input_shape="valid", raw_count=0, projected_count=0, projection_complete=True))

    def driver(backend):
        return InventoryAcquisitionDriver(tenant_id=tenant, backend=backend, catalog=catalog,
            authority=Authority(), sources=Sources(), collector=EmptyCollector(),
            service_budget=InventoryServicePlanBudget(factory, tenant, now=lambda: clock[0]), now=lambda: clock[0])

    async def usage():
        async with factory() as session:
            row = await session.scalar(select(ProspectingServiceReservationRow).where(
                ProspectingServiceReservationRow.tenant_id == tenant))
            return dict(row.execution_usage)

    with pytest.raises(asyncio.CancelledError):
        await driver(service).scan()
    first = await service.get_job(tenant, crashed[0])
    before = await usage()
    assert len(before) == 1
    assert len(calls) == int(crash == "unknown")
    assert await service.source_progress(tenant, first.id, first.lease_token) == (
        {first.source_plan[0]: "started"} if crash == "unknown" else {})
    if transition != "same":
        await store.assign(tenant, "basic" if transition == "downgrade" else "advanced",
            expected_version=state.version, actor_id="operator", now=NOW)
    clock[0] += timedelta(minutes=11)
    restarted = PostgresInventoryService(factory, tenant, catalog, now=lambda: clock[0])
    await driver(restarted).scan()
    current = await restarted.get_job(tenant, first.id)
    assert current.lease_token != first.lease_token
    after = await usage()
    key = next(iter(before))
    assert after[key]["amount"] == before[key]["amount"]
    assert (await store.get(tenant)).candidates_reserved == count
    assert len(calls) == len({source for source, _ in calls})
    if transition != "downgrade":
        assert [source for source, _ in calls] == list(first.source_plan)
        assert sum(v["amount"] for v in after.values()) == count
    else:
        # 原15预留不退款；降档仅余10上限，恢复未派发来源实际最多5，后续无新预算。
        assert calls == [(first.source_plan[0], 5 if crash == "reserved" else 15)]
        assert sum(v["amount"] for v in after.values()) == 15
    if crash == "reserved":
        assert current.failure_reason != "collector_failed"


async def test_persisted_old_intent_is_denied_at_actual_model_after_new_lease(inventory, monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from apps.scheduler_worker import inventory_model as module
    from shared.schemas.model_invocation import ModelGenerationError

    service, catalog, factory, tenant, _ = inventory
    clock = [datetime.now(UTC)]
    service.now = lambda: clock[0]
    await service.request(tenant, EmployeeId("emp"), InventoryQuery(product_tags=("hinges",),
        intent="password=placeholder"), 1, "old-input")
    first = await service.claim_job(tenant, timedelta(minutes=10))
    clock[0] += timedelta(minutes=11)
    backend = PostgresInventoryService(factory, tenant, catalog, now=lambda: clock[0])
    job = await backend.claim_job(tenant, timedelta(minutes=10))
    assert job.lease_token != first.lease_token and job.attempts == 2

    @asynccontextmanager
    async def employees(current_tenant):
        assert current_tenant == tenant
        yield SimpleNamespace(get_employee=AsyncMock(return_value=SimpleNamespace(
            tenant_id=tenant, employee_id=EmployeeId("emp"), user_id="usr", is_active=True, role="sales")))

    settings = SimpleNamespace(configuration_version="v1", api_key_file="never-read", codex_job_root="never-created",
        model="synthetic", limits=SimpleNamespace(timeout_seconds=1, max_output_tokens=100))
    monkeypatch.setattr(module, "resolve_task_model", lambda *_: settings)
    generator = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(text='{"candidates": []}')))
    monkeypatch.setattr(module, "build_model_composition", lambda **_: SimpleNamespace(generator=generator, aclose=AsyncMock()))
    model = module.InventoryDiscoveryModel(tenant_id=tenant, backend=backend, employees=employees,
        configuration=SimpleNamespace(authorize=AsyncMock()), settings=settings, resolver=None,
        sessions=factory, fingerprints=None, lease_owner="test")
    with pytest.raises(ModelGenerationError):
        await model.generate(job, "company_websites", None, {})
    generator.generate.assert_not_awaited()
