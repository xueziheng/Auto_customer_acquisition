"""SourcingServiceImpl 经真实 PostgreSQL UoW 的关键持久化与 Outbox 验证。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text
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
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingReviewCommand,
    SpecComparisonView,
)
from domains.sourcing.service import CandidateEvidenceSnapshot
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import OutboxEventRow, SourcingCaseRow
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    ProductId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingSupplyOptionId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

NOW = datetime(2026, 8, 30, 11, tzinfo=UTC)

_models = importlib.import_module("domains.sourcing.models")
CaseState = _models.CaseState
LadderCheck = _models.LadderCheck
LadderOutcome = _models.LadderOutcome
MatchLadderRung = _models.MatchLadderRung
SourcingSupplyOption = _models.SourcingSupplyOption
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
) -> None:
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
        artifact_id = new_id("art")
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
            _SealReadCandidates(
                self.candidates, self._read_complete, self._resume
            ),
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

    first = await service.open_case(tenant_id, _command(tenant_id, need_id), actor=actor)
    second = await service.open_case(tenant_id, _command(tenant_id, need_id), actor=actor)

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
            lambda bound_tenant: _BarrierUow(
                sf, bound_tenant, barrier=barrier
            ),
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
            queries=("hinge factory",),
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
                        OutboxEventRow.event_type
                        == "SourcingCandidatesVerified",
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
    case_id = await service.open_case(tenant_id, _command(tenant_id, need_id), actor=system)
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
            queries=("hinge factory",),
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
async def test_review_handoff_is_one_case_cas_and_terminal_snapshot_in_postgres(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    opportunity_id = OpportunityId(new_id("opp"))
    product_id = ProductId(new_id("prd"))
    await _seed_need(integration_engine, tenant_id, need_id)
    await _seed_handoff_dependencies(
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
    case_id = await service.open_case(tenant_id, _command(tenant_id, need_id), actor=system)
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
            queries=("hinge factory",),
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
    option = SourcingSupplyOption(
        option_id=SourcingSupplyOptionId(new_id("sop")),
        tenant_id=tenant_id,
        case_id=case_id,
        source=SupplyOptionSource.EXISTING_PRODUCT,
        product_id=product_id,
        supplier_candidate_id=None,
        is_qualified=True,
        created_at=NOW,
    )
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        await uow.options.add(tenant_id, option)
    await service.mark_candidates_ready(
        tenant_id, case_id, (option.option_id,), (), actor=system
    )
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        ready = await uow.cases.get(tenant_id, SourcingCaseId(case_id))
    assert ready is not None
    review = await service.review(
        tenant_id,
        case_id,
        SourcingReviewCommand(
            primary_option_id=option.option_id,
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
