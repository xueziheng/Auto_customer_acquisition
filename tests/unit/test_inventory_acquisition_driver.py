"""采集轮次有界且探索槽不可因先命中而跳过。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domains.prospecting.collection_result import CollectionResult
from domains.prospecting.projection_diagnostic import ProjectionSummary, SaveSummary
from domains.prospecting.service import AcquisitionJob, InventoryQuery, SourceState
from tests.unit.test_prospect_inventory import candidate

NOW = datetime.now(UTC)
JOB = AcquisitionJob(
    tenant_id="tenant",
    id="acj_test",
    employee_id="emp",
    query=InventoryQuery(country="KE", product_tags=("tricycles",)),
    requested_count=1,
    remaining_count=1,
    status="processing",
    attempts=1,
    created_at=NOW,
    updated_at=NOW,
    lease_until=NOW + timedelta(minutes=10),
    lease_token="lease",
)


def driver_type():
    import importlib

    try:
        return importlib.import_module(
            "apps.scheduler_worker.inventory_acquisition"
        ).InventoryAcquisitionDriver
    except ModuleNotFoundError:
        pytest.fail("RED: 采集driver尚未实现")


class Backend:
    def __init__(self):
        self.job = JOB
        self.calls = []
        self.remaining = 50

    async def initialize_source_plan(self, tenant, job, token, plan):
        self.job = self.job.model_copy(
            update={
                "source_plan": plan,
                "collection_deadline": NOW + timedelta(hours=24),
            }
        )
        return self.job

    async def source_progress(self, *args):
        return {}

    async def reserve_source(self, *args):
        return 480 // len(self.job.source_plan)

    async def status(self, tenant):
        return SimpleNamespace(remaining_today=self.remaining)

    async def claim_job(self, tenant, duration):
        self.calls.append("claim")
        return self.job

    async def get_job(self, tenant, job):
        return self.job

    async def fail_job(self, tenant, job, token, reason):
        self.calls.append(reason)

    async def record_source_attempt(
        self, tenant, job, token, source, outcome, count, *, diagnostic=None
    ):
        self.calls.append((source, outcome, count))
        self.last_diagnostic = diagnostic

    async def save_collected(self, tenant, job, token, candidates):
        return SaveSummary(total_count=3, inserted_fresh=3, query_match=3, fresh=3)

    async def deliver_job(self, tenant, job, token):
        self.calls.append("deliver")


class Authority:
    async def current_scope(self, tenant, employee):
        return SimpleNamespace(
            tenant_id=tenant, employee_id=employee, allowed_owners={employee}
        )


class Sources:
    async def list_sources(self, tenant):
        return tuple(
            SourceState(
                source_id=s, availability="available", cost_class="free", relevant=True
            )
            for s in ("company_websites", "industry_directories")
        )


class Catalog:
    async def fresh(self, query, now):
        return ()

    async def save_public(self, candidates):
        return 3


class Collector:
    def __init__(self):
        self.sources = []

    async def collect(self, job, source, guard, **kwargs):
        await guard()
        self.sources.append(source)
        return CollectionResult(
            (candidate(),) * 3,
            ProjectionSummary(
                input_shape="valid",
                raw_count=3,
                projected_count=3,
                projection_complete=True,
            ),
        )


async def test_exploration_and_counts_come_from_saved_new_domains():
    backend, collector = Backend(), Collector()
    driver = driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Catalog(),
        authority=Authority(),
        sources=Sources(),
        collector=collector,
        now=lambda: NOW,
    )
    assert await driver.scan() == 1
    assert len(collector.sources) == 2
    assert backend.calls[-1] == "deliver"
    assert [c[2] for c in backend.calls if isinstance(c, tuple)] == [3, 3]


async def test_quota_wait_has_no_claim_and_recovery_never_repeats_external_call():
    backend, collector = Backend(), Collector()
    driver = driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Catalog(),
        authority=Authority(),
        sources=Sources(),
        collector=collector,
        now=lambda: NOW,
    )
    backend.remaining = 0
    assert await driver.scan() == 0 and backend.calls == []
    backend.remaining = 1
    backend.job = JOB.model_copy(update={"attempts": 2})
    assert await driver.scan() == 1 and collector.sources == []
    assert backend.calls[-1] == "budget_exhausted"


async def test_expired_lease_does_not_write_or_dispatch():
    backend, collector = Backend(), Collector()
    backend.job = JOB.model_copy(update={"lease_until": NOW})
    driver = driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Catalog(),
        authority=Authority(),
        sources=Sources(),
        collector=collector,
        now=lambda: NOW,
    )
    assert await driver.scan() == 1
    assert backend.calls == ["claim"] and collector.sources == []


def test_canonical_bootstrap_exposes_explicit_inventory_factory():
    from apps.scheduler_worker.bootstrap import CanonicalSchedulerBootstrap

    assert "inventory_factory" in CanonicalSchedulerBootstrap.__dataclass_fields__, (
        "RED: standalone库存工厂未接线"
    )


async def test_failed_source_still_attempts_planned_exploration():
    backend = Backend()

    class FailingCollector(Collector):
        async def collect(self, job, source, guard, **kwargs):
            self.sources.append(source)
            if len(self.sources) == 1:
                raise ValueError("site unavailable")
            return CollectionResult(
                (candidate(),) * 3,
                ProjectionSummary(
                    input_shape="valid",
                    raw_count=3,
                    projected_count=3,
                    projection_complete=True,
                ),
            )

    collector = FailingCollector()
    driver = driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Catalog(),
        authority=Authority(),
        sources=Sources(),
        collector=collector,
        now=lambda: NOW,
    )
    await driver.scan()
    assert len(collector.sources) == 2
    assert ("industry_directories", "failed", 0) in backend.calls


async def test_failed_sources_preserve_safe_diagnostic_and_finish_without_budget_misreport():
    from shared.schemas.discovery_diagnostic import DiscoveryDiagnostic
    from shared.schemas.model_invocation import ModelGenerationError

    class FailedCollector:
        async def collect(self, *args, **kwargs):
            raise ModelGenerationError(
                "invalid_request",
                diagnostic=DiscoveryDiagnostic("provider", "invalid_request", 3),
            )

    backend = Backend()
    driver = driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Catalog(),
        authority=Authority(),
        sources=Sources(),
        collector=FailedCollector(),
        now=lambda: NOW,
    )
    await driver.scan()
    outcomes = [c[1] for c in backend.calls if isinstance(c, tuple)]
    assert outcomes == ["failed:provider:req:3", "failed:provider:req:3"]
    assert backend.calls[-1] == "collector_failed"
    assert "deliver" not in backend.calls


async def test_failed_collection_still_delivers_candidates_already_available():
    class FailedCollector:
        async def collect(self, *args, **kwargs):
            raise ValueError("PRIVATE")

    class ExistingCatalog(Catalog):
        async def fresh(self, query, now):
            return (object(),)

    backend = Backend()
    await driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=ExistingCatalog(),
        authority=Authority(),
        sources=Sources(),
        collector=FailedCollector(),
        now=lambda: NOW,
    ).scan()
    assert backend.calls[-1] == "deliver"


async def test_private_full_cache_is_evaluated_not_counted_as_collection_success():
    from domains.prospecting.inventory_fit import (
        FitCandidateVersion,
        InventoryFitAssessment,
        InventoryFitBatch,
        candidate_evidence_version,
        fit_query_fingerprint,
    )

    backend, collector = Backend(), Collector()
    backend.job = JOB.model_copy(
        update={
            "query": InventoryQuery(
                country="KE", product_tags=("hinges",), intent="仅有维修网络的经销商"
            )
        }
    )
    bad = candidate(
        id="pci_bad", country="KE", public_emails=(), evidence_text="Retail hinges"
    )
    good = candidate(
        id="pci_good",
        canonical_domain="repair.example",
        country="KE",
        public_emails=(),
        evidence_text="Dealer with repair network and hinges",
    )
    pool = [bad]
    evaluations = []

    class Fit:
        async def evaluate(self, job, batch, candidates, guard):
            await guard()
            evaluations.extend(c.id for c in candidates)
            return tuple(
                InventoryFitAssessment(
                    candidate_id=c.id,
                    evidence_version=candidate_evidence_version(c),
                    matched=c.id == good.id,
                    evidence_quote=c.evidence_text,
                    inference="适配推断",
                    extracted_by="synthetic",
                    extracted_at=NOW,
                )
                for c in candidates
            )

    class Cache(Catalog):
        async def fresh(self, *args):
            return tuple(pool)

    async def reserve(tenant, job, token, candidates):
        batch = InventoryFitBatch(
            query_fingerprint=fit_query_fingerprint(backend.job.query),
            sequence=len(backend.job.fit_batches),
            state="started",
            candidates=tuple(
                FitCandidateVersion(
                    candidate_id=c.id, evidence_version=candidate_evidence_version(c)
                )
                for c in candidates
            ),
        )
        backend.job = backend.job.model_copy(
            update={"fit_batches": (*backend.job.fit_batches, batch)}
        )
        return batch

    async def complete(tenant, job, token, sequence, decisions):
        batches = list(backend.job.fit_batches)
        batches[sequence] = batches[sequence].model_copy(
            update={"state": "complete", "assessments": decisions}
        )
        backend.job = backend.job.model_copy(update={"fit_batches": tuple(batches)})

    async def save(*args):
        if len(pool) == 1:
            pool.append(good)
        return SaveSummary(total_count=3, inserted_fresh=3, query_match=3, fresh=3)

    async def fit_pool(*args):
        return tuple(pool)

    backend.fit_pool = fit_pool
    backend.reserve_fit_batch, backend.complete_fit_batch, backend.save_collected = (
        reserve,
        complete,
        save,
    )
    driver = driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Cache(),
        authority=Authority(),
        sources=Sources(),
        collector=collector,
        fit_model=Fit(),
        now=lambda: NOW,
    )
    assert await driver.scan() == 1
    assert collector.sources
    assert evaluations == [bad.id, good.id]
    assert "deliver" in backend.calls


async def test_revoked_knowledge_blocks_reusing_completed_fit_before_delivery():
    from shared.errors import PermissionDenied

    backend, collector = Backend(), Collector()
    backend.job = JOB.model_copy(
        update={
            "query": InventoryQuery(
                product_tags=("hinges",),
                confirmed_refs=({"document_id": "doc", "revision_id": "rev"},),
            )
        }
    )

    class Revoked:
        async def read(self, *args):
            raise PermissionDenied("撤权")

    driver = driver_type()(
        tenant_id="tenant",
        backend=backend,
        catalog=Catalog(),
        authority=Authority(),
        sources=Sources(),
        collector=collector,
        context_reader=Revoked(),
        now=lambda: NOW,
    )
    assert await driver.scan() == 1
    assert "deliver" not in backend.calls and not collector.sources
    assert "collector_failed" in backend.calls

async def test_basic_plan_bounds_all_source_targets_instead_of_replenishing_fifty():
    class Budget:
        spent = 0
        async def limits(self, job): return {'candidates':10,'searches':10,'pages':10,'pages_per_company':2}
        async def remaining_candidates(self, job): return 10-self.spent
        async def reserve_execution(self, job, *, kind, key, amount, host=None):
            assert kind == 'candidates' and self.spent + amount <= 10
            self.spent += amount
        async def admit_source_candidates(self, job, source, candidates): pass
        async def fit_candidates(self, job, candidates): return candidates[:10]
    class BoundedCollector(Collector):
        def __init__(self):
            super().__init__()
            self.targets = []
        async def collect(self, job, source, guard, **kwargs):
            self.targets.append(kwargs['target_count'])
            return await super().collect(job,source,guard,**kwargs)
    budget, collector = Budget(), BoundedCollector()
    driver = driver_type()(tenant_id='tenant',backend=Backend(),catalog=Catalog(),authority=Authority(),sources=Sources(),collector=collector,service_budget=budget,now=lambda:NOW)
    await driver.scan()
    assert collector.targets == [5,5] and budget.spent == 10
