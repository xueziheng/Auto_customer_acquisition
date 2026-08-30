"""SourcingServiceImpl 经真实 PostgreSQL UoW 的关键持久化与 Outbox 验证。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.sourcing.errors import SourcingCaseConflictError
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.schemas import (
    CandidateSubmission,
    IndicativePriceTier,
    NeedFact,
    OpenSourcingCase,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingReviewCommand,
    SpecComparisonView,
)
from domains.sourcing.service import CandidateEvidenceSnapshot
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import OutboxEventRow, SourcingCaseRow, SourcingLadderCheckRow
from shared.errors import InvalidStateTransition, TransientError, ValidationError
from shared.events.catalog import NeedValidated
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    ProductId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType
from workflows.engine.runner import WorkflowRun

NOW = datetime(2026, 8, 30, 11, tzinfo=UTC)

_models = importlib.import_module("domains.sourcing.models")
CaseState = _models.CaseState
LadderCheck = _models.LadderCheck
LadderOutcome = _models.LadderOutcome
MatchLadderRung = _models.MatchLadderRung
SourcingSupplyOption = _models.SourcingSupplyOption
SpecComparison = _models.SpecComparison
SpecMatchLevel = _models.SpecMatchLevel
SupplyOptionSource = _models.SupplyOptionSource


def _service_type() -> type[Any]:
    try:
        return importlib.import_module(
            "domains.sourcing.service_impl"
        ).SourcingServiceImpl
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：SourcingServiceImpl 尚未实现（{exc}）")


async def _seed_need(
    engine: AsyncEngine, tenant_id: TenantId, need_id: ValidatedNeedId
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id, need_id, account_id, product_category, source_message_id, status, created_at) "
                "VALUES (:tenant, :need, 'account-service', CAST(:category AS jsonb), "
                "'message-service', 'sourcing_ready', :created_at)"
            ),
            {
                "tenant": tenant_id,
                "need": need_id,
                "category": '{"value":"hinges"}',
                "created_at": NOW,
            },
        )


async def _seed_handoff_dependencies(
    engine: AsyncEngine,
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
    opportunity_id: OpportunityId,
    product_id: ProductId,
) -> ArtifactId:
    artifact_id = ArtifactId(new_id("art"))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO opportunities "
                "(opportunity_id, tenant_id, account_id, account_name, country, need_id, product_category) "
                "VALUES (:opportunity, :tenant, :account, 'Buyer', 'US', :need, 'hinges')"
            ),
            {
                "opportunity": opportunity_id,
                "tenant": tenant_id,
                "account": new_id("acc"),
                "need": need_id,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO raw_artifacts "
                "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
                "VALUES (:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', :key, :created_at)"
            ),
            {
                "tenant": tenant_id,
                "artifact": artifact_id,
                "hash": "d" * 64,
                "key": f"raw/{tenant_id}/{artifact_id}",
                "created_at": NOW,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO products "
                "(tenant_id, product_id, pool, name_zh, name_en, category, normalized_category, "
                "sellable_markets, customizable, selling_points, known_issues, internal_cost_amount, "
                "internal_cost_currency, internal_cost_basis, internal_cost_unit, internal_cost_source_ref, moq, created_at) "
                "VALUES (:tenant, :product, 'formal', '铰链', 'Hinge', 'hinges', 'hinges', "
                "'[]', false, '[]', '[]', 1.25, 'USD', 'quoted supplier basis', 'piece', :artifact, 1000, :created_at)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "artifact": artifact_id,
                "created_at": NOW,
            },
        )
    return artifact_id


async def _seed_additional_product(
    engine: AsyncEngine,
    tenant_id: TenantId,
    product_id: ProductId,
) -> ArtifactId:
    artifact_id = ArtifactId(new_id("art"))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO raw_artifacts "
                "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
                "VALUES (:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', :key, :created_at)"
            ),
            {
                "tenant": tenant_id,
                "artifact": artifact_id,
                "hash": "e" * 64,
                "key": f"raw/{tenant_id}/{artifact_id}",
                "created_at": NOW,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO products "
                "(tenant_id, product_id, pool, name_zh, name_en, category, normalized_category, "
                "sellable_markets, customizable, selling_points, known_issues, internal_cost_amount, "
                "internal_cost_currency, internal_cost_basis, internal_cost_unit, internal_cost_source_ref, moq, created_at) "
                "VALUES (:tenant, :product, 'formal', '铰链 B', 'Hinge B', 'hinges', 'hinges', "
                "'[]', false, '[]', '[]', 1.50, 'USD', 'quoted supplier basis', 'piece', :artifact, 1000, :created_at)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "artifact": artifact_id,
                "created_at": NOW,
            },
        )
    return artifact_id


def _qualified_product_ladder_check(
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    product_ids: tuple[ProductId, ...],
    evidence_ref: ArtifactId,
) -> Any:
    frozen_ids = sorted(map(str, product_ids))
    return LadderCheck(
        check_id=new_id("slc"),
        tenant_id=tenant_id,
        case_id=case_id,
        sequence_number=1,
        rung=MatchLadderRung(1),
        outcome=LadderOutcome.QUALIFIED_SUPPLY_FOUND,
        input_snapshot={
            "category": "hinges",
            "qualified_product_ids": frozen_ids,
            "product_spec_evidence": {
                product_id: {"product_category": str(evidence_ref)}
                for product_id in frozen_ids
            },
        },
        input_snapshot_hash="b" * 64,
        conclusion="internal_product_qualified",
        match_object_type="product",
        match_object_id=frozen_ids[0],
        spec_comparisons=tuple(
            SpecComparison(
                spec_name="product_category",
                required="hinges",
                offered="hinges",
                level=SpecMatchLevel.EXACT,
                product_id=ProductId(product_id),
                evidence_ref=evidence_ref,
            )
            for product_id in frozen_ids
        ),
        evidence_refs=(evidence_ref,),
        checked_by=EmployeeId("untrusted"),
        checked_at=NOW,
    )


@pytest.mark.asyncio
async def test_invalid_product_evidence_mapping_rolls_back_real_postgres(
    integration_engine: AsyncEngine,
) -> None:
    """持久化前独立门禁失败时，Case 与 LadderCheck 都不得留下部分状态。"""

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    product_id = ProductId(new_id("prd"))
    opportunity_id = OpportunityId(new_id("opp"))
    await _seed_need(integration_engine, tenant_id, need_id)
    evidence_ref = await _seed_handoff_dependencies(
        integration_engine, tenant_id, need_id, opportunity_id, product_id
    )
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    case_id = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )
    valid = _qualified_product_ladder_check(
        tenant_id, case_id, (product_id,), evidence_ref
    )
    snapshot = dict(valid.input_snapshot)
    snapshot["product_spec_evidence"] = {
        str(product_id): {"product_category": "art-forged"}
    }
    invalid = valid.__class__(
        **{**valid.__dict__, "input_snapshot": snapshot}
    )

    with pytest.raises(ValidationError, match="证据映射"):
        await service.record_ladder_check(
            tenant_id, case_id, invalid, actor=system
        )

    async with sf() as session:
        case = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
        count = await session.scalar(
            select(func.count()).select_from(SourcingLadderCheckRow).where(
                SourcingLadderCheckRow.tenant_id == tenant_id,
                SourcingLadderCheckRow.case_id == case_id,
            )
        )
    assert case is not None and case.state == CaseState.OPENED.value
    assert count == 0


def _command(tenant_id: TenantId, need_id: ValidatedNeedId) -> OpenSourcingCase:
    provenance = ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="message-service",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-boss"),
        confirmed_at=NOW,
    )
    return OpenSourcingCase(
        need=SourcingNeedSnapshot(
            need_id=need_id,
            completeness=3,
            derivation_version="need-completeness-v1",
            product_category=NeedFact(value="hinges", provenance=provenance),
            quantity=NeedFact(value=5000, provenance=provenance),
            snapshot_hash="a" * 64,
        ),
        trigger_key=f"sourcing-case:v2:{tenant_id}:{need_id}",
    )


async def _seed_candidate_artifact(
    engine: AsyncEngine, tenant_id: TenantId, artifact_id: ArtifactId
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO raw_artifacts "
                "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
                "VALUES (:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', :key, :created_at)"
            ),
            {
                "tenant": tenant_id,
                "artifact": artifact_id,
                "hash": "c" * 64,
                "key": f"raw/{tenant_id}/{artifact_id}",
                "created_at": NOW,
            },
        )


def _candidate_submission(artifact_id: ArtifactId) -> CandidateSubmission:
    provenance = ProvenanceSummary(
        source_type=SourceType.WEB_PAGE,
        source_id="candidate-page",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=None,
        confirmed_at=None,
    )
    facts = {
        name: SourcingObservedFact(
            value=f"offered-{name}",
            provenance=provenance,
            evidence_ref=artifact_id,
        )
        for name in ("product_type", "material", "size", "model")
    }
    facts.update(
        {
            "moq": SourcingObservedFact(
                value=1000, provenance=provenance, evidence_ref=artifact_id
            ),
            "price_unit": SourcingObservedFact(
                value="piece", provenance=provenance, evidence_ref=artifact_id
            ),
            "currency": SourcingObservedFact(
                value="USD", provenance=provenance, evidence_ref=artifact_id
            ),
        }
    )
    return CandidateSubmission(
        supplier_name="Factory A",
        product_title="Stainless hinge",
        source_platform="official_site",
        specs=tuple(
            SpecComparisonView(
                spec_name=name,
                required=f"required-{name}",
                offered=f"offered-{name}",
                level="exact",
            )
            for name in ("product_type", "material", "size", "model")
        ),
        observed_facts=facts,
        match_inferences={
            "fit": SourcingMatchInference(
                value="四项均匹配",
                based_on=(artifact_id,),
                inferred_by="human",
                inferred_at=NOW,
            )
        },
        indicative_price_tiers=(
            IndicativePriceTier(
                minimum_quantity=1000,
                amount=Decimal("1.25"),
                currency="USD",
                unit="piece",
                provenance=provenance,
                evidence_ref=artifact_id,
            ),
        ),
        moq=1000,
        price_unit="piece",
        currency="USD",
        evidence_url="https://factory.example/hinge",
        evidence_hash="c" * 64,
        evidence_artifact_ref=str(artifact_id),
    )


class _UnusedEvidenceReader:
    async def read_verified(self, tenant_id: TenantId, artifact_id: Any) -> Any:
        raise AssertionError("开案路径不得读取候选 Evidence")


class _NeedReader:
    def __init__(self, snapshot: SourcingNeedSnapshot) -> None:
        self._snapshot = snapshot

    async def read(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingNeedSnapshot:
        assert need_id == self._snapshot.need_id
        return self._snapshot


class _NeverRunStep:
    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, object]]:
        raise AssertionError(f"本测试只验证 durable start，不应 poll {run.run_id}")


class _TransientFirstStart:
    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self.calls = 0

    async def start(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise TransientError(
                "postgres://private-user:secret@workflow-db/internal",
                context={"credential": "raw-token"},
            )
        return await self._engine.start(*args, **kwargs)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class _FixedEvidenceReader:
    def __init__(self, projection: CandidateEvidenceSnapshot) -> None:
        self._projection = projection

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> CandidateEvidenceSnapshot:
        assert tenant_id == self._projection.tenant_id
        assert artifact_id == self._projection.artifact_id
        return self._projection


class _TwoPartyBarrier:
    def __init__(self) -> None:
        self._arrived = 0
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            self._arrived += 1
            if self._arrived == 2:
                self._event.set()
        await self._event.wait()


class _BarrierCases:
    def __init__(self, inner: Any, barrier: _TwoPartyBarrier) -> None:
        self._inner = inner
        self._barrier = barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get_by_trigger(self, tenant_id: TenantId, trigger_key: str) -> Any:
        result = await self._inner.get_by_trigger(tenant_id, trigger_key)
        await self._barrier.wait()
        return result

    async def get_or_create(self, tenant_id: TenantId, case: Any) -> Any:
        await self._barrier.wait()
        return await self._inner.get_or_create(tenant_id, case)


class _BarrierUow(SqlAlchemySourcingUnitOfWork):
    def __init__(self, *args: Any, barrier: _TwoPartyBarrier, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._barrier = barrier

    async def __aenter__(self) -> Any:
        entered = await super().__aenter__()
        self.cases = cast(Any, _BarrierCases(self.cases, self._barrier))
        return entered


class _SealReadCandidates:
    def __init__(
        self, inner: Any, read_complete: asyncio.Event, resume: asyncio.Event
    ) -> None:
        self._inner = inner
        self._read_complete = read_complete
        self._resume = resume

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def list_for_case(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.list_for_case(*args, **kwargs)
        self._read_complete.set()
        await self._resume.wait()
        return result


class _SealReadUow(SqlAlchemySourcingUnitOfWork):
    def __init__(
        self,
        *args: Any,
        read_complete: asyncio.Event,
        resume: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._read_complete = read_complete
        self._resume = resume

    async def __aenter__(self) -> Any:
        entered = await super().__aenter__()
        self.candidates = cast(
            Any,
            _SealReadCandidates(self.candidates, self._read_complete, self._resume),
        )
        return entered


@pytest.mark.asyncio
async def test_open_case_is_idempotent_and_event_is_atomic_in_postgres(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    await _seed_need(integration_engine, tenant_id, need_id)
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    actor = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )

    first = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=actor
    )
    second = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=actor
    )

    async with sf() as session:
        case_count = await session.scalar(
            select(func.count())
            .select_from(SourcingCaseRow)
            .where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.need_id == need_id,
            )
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCaseOpened",
            )
        )
    assert first == second
    assert case_count == 1
    assert event_count == 1


@pytest.mark.asyncio
async def test_outbox_retries_sanitized_start_failure_and_recovers_one_real_run(
    integration_engine: AsyncEngine,
) -> None:
    """开案已提交而 Workflow Engine 临时失败时，Outbox 必须可恢复且不泄密。"""

    from apps.scheduler_worker.sourcing_events import SourcingTriggerHandler
    from infra.db.outbox import PostgresEventBus
    from infra.db.outbox_delivery import OutboxDeliverer
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from workflows.sourcing_case.flow import build_sourcing_case_definition

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    await _seed_need(integration_engine, tenant_id, need_id)
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    clock = _Clock(NOW)
    definition = build_sourcing_case_definition()
    never_run = _NeverRunStep()
    real_engine = PostgresWorkflowEngine(
        factory,
        {step.handler_ref: never_run for step in definition.steps},
        now=clock.now,
    )
    real_engine.register(definition)
    flaky_engine = _TransientFirstStart(real_engine)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    sourcing = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(factory, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=clock.now,
    )
    command = _command(tenant_id, need_id)
    handler = SourcingTriggerHandler(
        engine=flaky_engine,
        sourcing=sourcing,
        need_reader=_NeedReader(command.need),
        tenant_id=tenant_id,
        sourcing_actor=system,
    )
    deliverer = OutboxDeliverer(factory, tenant_id, now=clock.now)
    deliverer.register_handler(
        NeedValidated,
        "sourcing_case.v2.need_validated",
        cast(Any, handler),
    )
    session = factory()
    try:
        await PostgresEventBus(session, tenant_id, now=clock.now).publish(
            NeedValidated(
                tenant_id=tenant_id,
                occurred_at=clock.now(),
                need_id=need_id,
                category="hinges",
                evidence_level=EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING,
                completeness=3,
            )
        )
        await session.commit()
    finally:
        await session.close()

    await deliverer.drain()
    async with integration_engine.connect() as connection:
        first = (
            await connection.execute(
                text(
                    "SELECT e.status, e.last_error, d.status, d.attempts, d.last_error "
                    "FROM outbox_events e JOIN outbox_deliveries d "
                    "ON d.tenant_id=e.tenant_id AND d.event_id=e.event_id "
                    "WHERE e.tenant_id=:tenant AND e.event_type='NeedValidated' "
                    "AND e.event_payload->>'need_id'=:need"
                ),
                {"tenant": str(tenant_id), "need": str(need_id)},
            )
        ).one()
        case_count = await connection.scalar(
            text(
                "SELECT count(*) FROM sourcing_cases "
                "WHERE tenant_id=:tenant AND need_id=:need"
            ),
            {"tenant": str(tenant_id), "need": str(need_id)},
        )
        case_id = await connection.scalar(
            text(
                "SELECT case_id FROM sourcing_cases "
                "WHERE tenant_id=:tenant AND need_id=:need"
            ),
            {"tenant": str(tenant_id), "need": str(need_id)},
        )
        run_count = await connection.scalar(
            text(
                "SELECT count(*) FROM workflow_runs "
                "WHERE tenant_id=:tenant AND workflow_type='sourcing_case'"
            ),
            {"tenant": str(tenant_id)},
        )
    assert first == ("pending", "TransientError", "pending", 1, "TransientError")
    assert case_count == 1
    assert run_count == 0

    clock.value = NOW + timedelta(seconds=31)
    await deliverer.drain()
    async with integration_engine.connect() as connection:
        recovered = (
            await connection.execute(
                text(
                    "SELECT e.status, e.last_error, d.status, d.attempts, d.last_error "
                    "FROM outbox_events e JOIN outbox_deliveries d "
                    "ON d.tenant_id=e.tenant_id AND d.event_id=e.event_id "
                    "WHERE e.tenant_id=:tenant AND e.event_type='NeedValidated' "
                    "AND e.event_payload->>'need_id'=:need"
                ),
                {"tenant": str(tenant_id), "need": str(need_id)},
            )
        ).one()
        case_count = await connection.scalar(
            text(
                "SELECT count(*) FROM sourcing_cases "
                "WHERE tenant_id=:tenant AND need_id=:need"
            ),
            {"tenant": str(tenant_id), "need": str(need_id)},
        )
        runs = (
            await connection.execute(
                text(
                    "SELECT status, workflow_version, subject_ref, idempotency_key "
                    "FROM workflow_runs WHERE tenant_id=:tenant "
                    "AND workflow_type='sourcing_case'"
                ),
                {"tenant": str(tenant_id)},
            )
        ).all()
    assert recovered == ("delivered", None, "delivered", 1, None)
    assert case_count == 1
    assert runs == [
        (
            "running",
            2,
            case_id,
            f"sourcing-case:v2:{tenant_id}:{need_id}",
        )
    ]
    assert flaky_engine.calls == 2


@pytest.mark.asyncio
async def test_concurrent_open_returns_one_canonical_case_and_event(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    await _seed_need(integration_engine, tenant_id, need_id)
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    barrier = _TwoPartyBarrier()
    actor = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")

    def make_service() -> Any:
        return _service_type()(
            lambda bound_tenant: _BarrierUow(sf, bound_tenant, barrier=barrier),
            Phase2SourcingAuthorizer(tenant_id),
            _UnusedEvidenceReader(),
            now=lambda: NOW,
        )

    first, second = await asyncio.gather(
        make_service().open_case(tenant_id, _command(tenant_id, need_id), actor=actor),
        make_service().open_case(tenant_id, _command(tenant_id, need_id), actor=actor),
    )
    async with sf() as session:
        cases = await session.scalar(
            select(func.count())
            .select_from(SourcingCaseRow)
            .where(SourcingCaseRow.tenant_id == tenant_id)
        )
        events = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCaseOpened",
            )
        )
    assert first == second
    assert cases == 1
    assert events == 1


@pytest.mark.asyncio
async def test_candidate_seal_cas_cannot_publish_a_stale_subset(
    integration_engine: AsyncEngine,
) -> None:
    """T1 读到 A 后 T2 提交 B；T1 必须 CAS 失败，重试只能封存 A+B。"""

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    artifact_id = ArtifactId(new_id("art"))
    await _seed_need(integration_engine, tenant_id, need_id)
    await _seed_candidate_artifact(integration_engine, tenant_id, artifact_id)
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    boss = SourcingActor("emp-boss", tenant_id, SourcingScope.TENANT, "boss")
    sourcing = SourcingActor(
        "emp-sourcing", tenant_id, SourcingScope.TENANT, "sourcing"
    )
    projection = CandidateEvidenceSnapshot(
        tenant_id=tenant_id,
        artifact_id=artifact_id,
        canonical_url="https://factory.example/hinge",
        content_hash="c" * 64,
        observed_at=NOW,
    )

    def service_with(factory: Any) -> Any:
        return _service_type()(
            factory,
            Phase2SourcingAuthorizer(tenant_id),
            _FixedEvidenceReader(projection),
            now=lambda: NOW,
        )

    normal = service_with(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant)
    )
    case_id = await normal.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )
    for rung in range(1, 6):
        await normal.record_ladder_check(
            tenant_id,
            case_id,
            LadderCheck(
                check_id=new_id("slc"),
                tenant_id=tenant_id,
                case_id=case_id,
                sequence_number=rung,
                rung=MatchLadderRung(rung),
                outcome=LadderOutcome.NO_QUALIFIED_SUPPLY,
                input_snapshot={"category": "hinges"},
                input_snapshot_hash="b" * 64,
                conclusion="无合格供给",
                match_object_type=None,
                match_object_id=None,
                spec_comparisons=(),
                evidence_refs=(),
                checked_by=EmployeeId("untrusted"),
                checked_at=NOW,
            ),
            actor=system,
        )
    plan = await normal.save_public_plan(
        tenant_id,
        case_id,
        PublicSourcingPlanCommand(
            plan_id=SourcingPlanId(new_id("spl")),
            case_id=case_id,
            target_countries=("US",),
            product_category="hinges",
            queries=(
                PublicSourcingQuery(
                    query_text="hinge factory", target_country="US"
                ),
            ),
            max_search_queries=1,
            max_pages_read=3,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=100,
            worst_case_credits=1,
            version=1,
            expected_case_version=6,
        ),
        actor=boss,
    )
    await normal.confirm_public_plan(
        tenant_id, plan.plan_id, plan.plan_hash, actor=boss
    )
    candidate_a = await normal.submit_candidate(
        tenant_id, case_id, _candidate_submission(artifact_id), actor=sourcing
    )

    read_complete = asyncio.Event()
    resume = asyncio.Event()
    sealing = service_with(
        lambda bound_tenant: _SealReadUow(
            sf,
            bound_tenant,
            read_complete=read_complete,
            resume=resume,
        )
    )
    seal_task = asyncio.create_task(
        sealing.mark_candidates_verified(
            tenant_id, case_id, (candidate_a,), actor=system
        )
    )
    await asyncio.wait_for(read_complete.wait(), timeout=5)
    candidate_b = await normal.submit_candidate(
        tenant_id, case_id, _candidate_submission(artifact_id), actor=sourcing
    )
    resume.set()
    with pytest.raises(SourcingCaseConflictError):
        await asyncio.wait_for(seal_task, timeout=5)

    expected_ids = tuple(sorted((candidate_a, candidate_b), key=str))
    verified = await normal.mark_candidates_verified(
        tenant_id, case_id, expected_ids, actor=system
    )
    async with sf() as session:
        row = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
        events = list(
            (
                await session.execute(
                    select(OutboxEventRow).where(
                        OutboxEventRow.tenant_id == tenant_id,
                        OutboxEventRow.event_type == "SourcingCandidatesVerified",
                    )
                )
            ).scalars()
        )
    assert row is not None
    assert tuple(row.sealed_candidate_ids) == tuple(map(str, expected_ids))
    assert row.candidate_set_hash == verified.candidate_set_hash
    assert len(events) == 1
    assert tuple(events[0].event_payload["candidate_ids"]) == tuple(
        map(str, expected_ids)
    )


@pytest.mark.asyncio
async def test_ladder_and_plan_confirmation_persist_case_state_with_exact_hash(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    await _seed_need(integration_engine, tenant_id, need_id)
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    boss = SourcingActor("emp-boss", tenant_id, SourcingScope.TENANT, "boss")
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    case_id = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )
    for rung in range(1, 6):
        await service.record_ladder_check(
            tenant_id,
            case_id,
            LadderCheck(
                check_id=new_id("slc"),
                tenant_id=tenant_id,
                case_id=case_id,
                sequence_number=rung,
                rung=MatchLadderRung(rung),
                outcome=LadderOutcome.NO_QUALIFIED_SUPPLY,
                input_snapshot={"category": "hinges"},
                input_snapshot_hash="b" * 64,
                conclusion="无合格供给",
                match_object_type=None,
                match_object_id=None,
                spec_comparisons=(),
                evidence_refs=(),
                checked_by=EmployeeId("untrusted"),
                checked_at=NOW,
            ),
            actor=system,
        )
    plan = await service.save_public_plan(
        tenant_id,
        case_id,
        PublicSourcingPlanCommand(
            plan_id=SourcingPlanId(new_id("spl")),
            case_id=case_id,
            target_countries=("US",),
            product_category="hinges",
            queries=(
                PublicSourcingQuery(
                    query_text="hinge factory", target_country="US"
                ),
            ),
            max_search_queries=1,
            max_pages_read=3,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=100,
            worst_case_credits=1,
            version=1,
            expected_case_version=6,
        ),
        actor=boss,
    )
    await service.confirm_public_plan(
        tenant_id, plan.plan_id, plan.plan_hash, actor=boss
    )
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        case = await uow.cases.get(tenant_id, SourcingCaseId(case_id))
        stored = await uow.plans.get(tenant_id, plan.plan_id)
    assert case is not None and case.state is CaseState.VERIFYING
    assert case.active_search_plan_id == plan.plan_id
    assert case.version == 7
    assert stored is not None and stored.authorized_plan_hash == plan.plan_hash


@pytest.mark.asyncio
async def test_existing_product_option_is_canonical_under_concurrent_registration(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    opportunity_id = OpportunityId(new_id("opp"))
    product_id = ProductId(new_id("prd"))
    await _seed_need(integration_engine, tenant_id, need_id)
    evidence_ref = await _seed_handoff_dependencies(
        integration_engine, tenant_id, need_id, opportunity_id, product_id
    )
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    case_id = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )
    missing_product = ProductId("prd_zz_missing")
    await service.record_ladder_check(
        tenant_id,
        case_id,
        _qualified_product_ladder_check(
            tenant_id,
            case_id,
            (product_id, missing_product),
            evidence_ref,
        ),
        actor=system,
    )
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        stored_checks = await uow.checks.list_for_case(tenant_id, case_id)
    stored_comparison = stored_checks[0].spec_comparisons[0]
    assert stored_comparison.product_id == product_id
    assert stored_comparison.evidence_ref == evidence_ref
    assert stored_checks[0].input_snapshot["product_spec_evidence"] == {
        str(product_id): {"product_category": str(evidence_ref)},
        str(missing_product): {"product_category": str(evidence_ref)},
    }

    with pytest.raises(IntegrityError):
        await service.register_existing_product_option(
            tenant_id, case_id, missing_product, actor=system
        )
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        rolled_back_case = await uow.cases.get(tenant_id, case_id)
        rolled_back_options = await uow.options.list_for_case(tenant_id, case_id)
    assert (
        rolled_back_case is not None and rolled_back_case.state is CaseState.DISCOVERING
    )
    assert rolled_back_options == []

    first, second = await asyncio.gather(
        service.register_existing_product_option(
            tenant_id, case_id, product_id, actor=system
        ),
        service.register_existing_product_option(
            tenant_id, case_id, product_id, actor=system
        ),
    )

    assert first == second
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        case = await uow.cases.get(tenant_id, case_id)
        options = await uow.options.list_for_case(tenant_id, case_id)
    assert case is not None and case.state is CaseState.VERIFYING
    assert len(options) == 1 and options[0].product_id == product_id


@pytest.mark.asyncio
async def test_ready_freeze_is_exact_concurrent_and_replay_safe_in_postgres(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    opportunity_id = OpportunityId(new_id("opp"))
    first_product = ProductId(new_id("prd"))
    second_product = ProductId(new_id("prd"))
    await _seed_need(integration_engine, tenant_id, need_id)
    first_evidence = await _seed_handoff_dependencies(
        integration_engine,
        tenant_id,
        need_id,
        opportunity_id,
        first_product,
    )
    await _seed_additional_product(integration_engine, tenant_id, second_product)
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(factory, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    case_id = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )
    await service.record_ladder_check(
        tenant_id,
        case_id,
        _qualified_product_ladder_check(
            tenant_id,
            case_id,
            (first_product, second_product),
            first_evidence,
        ),
        actor=system,
    )
    first_option = await service.register_existing_product_option(
        tenant_id, case_id, first_product, actor=system
    )

    with pytest.raises(ValidationError, match="现有产品.*冻结集合"):
        await service.mark_candidates_ready(
            tenant_id, case_id, (first_option,), (), actor=system
        )

    second_a, second_b = await asyncio.gather(
        service.register_existing_product_option(
            tenant_id, case_id, second_product, actor=system
        ),
        service.register_existing_product_option(
            tenant_id, case_id, second_product, actor=system
        ),
    )
    assert second_a == second_b
    ready_options = tuple(sorted((first_option, second_a), key=str))
    await service.mark_candidates_ready(
        tenant_id, case_id, ready_options, (), actor=system
    )
    await service.mark_candidates_ready(
        tenant_id, case_id, ready_options, (), actor=system
    )
    assert (
        await service.register_existing_product_option(
            tenant_id, case_id, first_product, actor=system
        )
        == first_option
    )

    async with factory() as session:
        await session.execute(
            text(
                "DELETE FROM sourcing_supply_options "
                "WHERE tenant_id=:tenant AND option_id=:option"
            ),
            {"tenant": str(tenant_id), "option": str(second_a)},
        )
        await session.commit()
    with pytest.raises(InvalidStateTransition, match="就绪后禁止新增"):
        await service.register_existing_product_option(
            tenant_id, case_id, second_product, actor=system
        )

    async with factory() as session:
        ready_event_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCandidatesReady",
            )
        )
    assert ready_event_count == 1


@pytest.mark.asyncio
async def test_review_handoff_is_one_case_cas_and_terminal_snapshot_in_postgres(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    opportunity_id = OpportunityId(new_id("opp"))
    product_id = ProductId(new_id("prd"))
    await _seed_need(integration_engine, tenant_id, need_id)
    evidence_ref = await _seed_handoff_dependencies(
        integration_engine, tenant_id, need_id, opportunity_id, product_id
    )
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    boss = SourcingActor("emp-boss", tenant_id, SourcingScope.TENANT, "boss")
    sourcing = SourcingActor(
        "emp-sourcing", tenant_id, SourcingScope.TENANT, "sourcing"
    )
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    case_id = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )
    await service.record_ladder_check(
        tenant_id,
        case_id,
        _qualified_product_ladder_check(
            tenant_id,
            case_id,
            (product_id,),
            evidence_ref,
        ),
        actor=system,
    )
    option_id = await service.register_existing_product_option(
        tenant_id, case_id, product_id, actor=system
    )
    await service.mark_candidates_ready(
        tenant_id, case_id, (option_id,), (), actor=system
    )
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        ready = await uow.cases.get(tenant_id, SourcingCaseId(case_id))
    assert ready is not None
    review = await service.review(
        tenant_id,
        case_id,
        SourcingReviewCommand(
            primary_option_id=option_id,
            alternate_option_ids=(),
            reason="内部成本证据完整",
            expected_case_version=ready.version,
        ),
        actor=sourcing,
    )
    await service.confirm_review(tenant_id, review.review_id, actor=boss)
    snapshot = await service.hand_to_costing(
        tenant_id, case_id, opportunity_id, actor=system
    )
    async with sf() as session:
        row = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
        handed_event_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCaseHandedToCosting",
            )
        )
    assert row is not None
    assert row.state == "handed_to_costing"
    assert row.version == review.expected_case_version + 1
    assert snapshot.opportunity_id == opportunity_id
    assert handed_event_count == 1
