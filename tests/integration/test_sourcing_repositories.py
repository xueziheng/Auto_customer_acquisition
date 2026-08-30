"""Sourcing V2 仓储、CAS 与事务原子性合同。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.sourcing.schemas import (
    IndicativePriceTier,
    NeedFact,
    PublicSourcingPlanCommand,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingReviewCommand,
    SourcingSupplierClaim,
)
from infra.db.tables import (
    OutboxEventRow,
    SourcingCaseRow,
)
from shared.errors import ValidationError
from shared.events.catalog import SourcingCandidatesReady
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


class _CommitFailingSession(AsyncSession):
    """在真实 flush 后模拟数据库提交失败，验证 UoW 必须 rollback。"""

    async def commit(self) -> None:
        await self.flush()
        raise RuntimeError("simulated commit failure")


def _symbol(module: str, name: str) -> Any:
    """延迟导入待实现边界，使 RED 是可读的合同失败而非收集失败。"""

    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


CaseState = _symbol("domains.sourcing.models", "CaseState")
EvidenceSnapshot = _symbol("domains.sourcing.models", "EvidenceSnapshot")
MatchExplanation = _symbol("domains.sourcing.models", "MatchExplanation")
MatchLadderRung = _symbol("domains.sourcing.models", "MatchLadderRung")
PublicSourcingPlan = _symbol("domains.sourcing.models", "PublicSourcingPlan")
SourcingCase = _symbol("domains.sourcing.models", "SourcingCase")
SourcingStopCode = _symbol("domains.sourcing.models", "SourcingStopCode")
SourcingStopDetail = _symbol("domains.sourcing.models", "SourcingStopDetail")
SourcingStopStage = _symbol("domains.sourcing.models", "SourcingStopStage")
SourcingSupplyOption = _symbol("domains.sourcing.models", "SourcingSupplyOption")
SpecComparison = _symbol("domains.sourcing.models", "SpecComparison")
SpecMatchLevel = _symbol("domains.sourcing.models", "SpecMatchLevel")
SupplierCandidate = _symbol("domains.sourcing.models", "SupplierCandidate")
SupplyOptionSource = _symbol("domains.sourcing.models", "SupplyOptionSource")


@pytest_asyncio.fixture
async def sourcing_engine(db_url: str) -> AsyncEngine:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _tenant() -> TenantId:
    return TenantId(new_id("tn"))


def _artifact() -> ArtifactId:
    return ArtifactId(new_id("art"))


def _need_snapshot(need_id: ValidatedNeedId, artifact_id: ArtifactId) -> SourcingNeedSnapshot:
    provenance = ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-need",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-boss"),
        confirmed_at=NOW,
    )
    return SourcingNeedSnapshot(
        need_id=need_id,
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(value="hinges", provenance=provenance),
        quantity=NeedFact(value=5000, provenance=provenance),
        snapshot_hash="a" * 64,
    )


async def _seed_need(engine: AsyncEngine, tenant_id: TenantId, need_id: ValidatedNeedId) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id, need_id, account_id, product_category, source_message_id, status, created_at) "
                "VALUES (:tenant, :need, 'account-a', CAST(:category AS jsonb), "
                "'message-a', 'sourcing_ready', :created_at)"
            ),
            {
                "tenant": tenant_id,
                "need": need_id,
                "category": '{"value":"hinges"}',
                "created_at": NOW,
            },
        )


async def _seed_artifact(
    engine: AsyncEngine,
    tenant_id: TenantId,
    artifact_id: ArtifactId,
    *,
    content_hash: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO raw_artifacts "
                "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, "
                "object_key, uploaded_at) VALUES "
                "(:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', "
                ":object_key, :uploaded_at)"
            ),
            {
                "tenant": tenant_id,
                "artifact": artifact_id,
                "hash": content_hash,
                "object_key": f"raw/{tenant_id}/{artifact_id}",
                "uploaded_at": NOW,
            },
        )


async def _seed_opportunity(
    engine: AsyncEngine,
    tenant_id: TenantId,
    opportunity_id: OpportunityId,
    need_id: ValidatedNeedId,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO opportunities "
                "(opportunity_id, tenant_id, account_id, account_name, country, "
                "need_id, product_category) VALUES "
                "(:opportunity, :tenant, :account, 'Buyer A', 'US', :need, 'hinges')"
            ),
            {
                "opportunity": opportunity_id,
                "tenant": tenant_id,
                "account": new_id("acc"),
                "need": need_id,
            },
        )


def _case(
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
    case_id: SourcingCaseId,
    artifact_id: ArtifactId,
) -> SourcingCase:
    return SourcingCase(
        case_id=case_id,
        tenant_id=tenant_id,
        need_id=need_id,
        opened_at=NOW,
        workflow_version=2,
        trigger_key=f"sourcing-v2:{need_id}",
        need_snapshot=_need_snapshot(need_id, artifact_id),
        need_snapshot_hash="a" * 64,
        state_changed_at=NOW,
        stop_code=SourcingStopCode.PROVIDER_TIMEOUT,
        stop_detail=SourcingStopDetail(
            stage=SourcingStopStage.PROVIDER,
            query_index=None,
            provider_http_status=None,
        ),
    )


async def _advance_case_to_candidates_ready(
    uow_type: Any,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    case_id: SourcingCaseId,
) -> None:
    """通过真实 Case CAS 逐步推进到审核可绑定的 version 4。"""

    for offset, target in enumerate(
        (CaseState.DISCOVERING, CaseState.VERIFYING, CaseState.CANDIDATES_READY),
        start=1,
    ):
        async with uow_type(session_factory, tenant_id) as uow:
            case = await uow.cases.get(tenant_id, case_id)
            assert case is not None
            case.transition_to(target, changed_at=NOW + timedelta(seconds=offset))
            await uow.cases.update(tenant_id, case)


def _plan(tenant_id: TenantId, case_id: SourcingCaseId) -> PublicSourcingPlan:
    plan_id = SourcingPlanId(new_id("spl"))
    return PublicSourcingPlan.create(
        tenant_id,
        PublicSourcingPlanCommand(
            plan_id=plan_id,
            case_id=case_id,
            target_countries=("US",),
            product_category="hinges",
            queries=("hinge manufacturer",),
            max_search_queries=1,
            max_pages_read=3,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=100,
            worst_case_credits=1,
            version=1,
            expected_case_version=1,
        ),
        created_at=NOW,
    )


def _candidate(
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    candidate_id: SupplierCandidateId,
    evidence: tuple[EvidenceSnapshot, ...],
) -> SupplierCandidate:
    provenance = ProvenanceSummary(
        source_type=SourceType.WEB_PAGE,
        source_id="page-field",
        extracted_by="extractor-v1",
        extracted_at=NOW,
        confirmed_by=None,
        confirmed_at=None,
    )
    specs = [
        SpecComparison(name, "required", "offered", SpecMatchLevel.EXACT)
        for name in ("product_type", "material", "size", "model")
    ]
    fact = SourcingObservedFact(
        value="offered", provenance=provenance, evidence_ref=ArtifactId(evidence[0].artifact_ref)
    )
    claim = SourcingSupplierClaim(
        value="offered", provenance=provenance, evidence_ref=ArtifactId(evidence[0].artifact_ref)
    )
    inference = SourcingMatchInference(
        value="可按已确认规格供货",
        based_on=(ArtifactId(evidence[0].artifact_ref),),
        inferred_by="extractor-v1",
        inferred_at=NOW,
    )
    facts = {
        name: fact for name in ("product_type", "material", "size", "model")
    }
    facts.update(
        {
            "moq": SourcingObservedFact(
                value=1000,
                provenance=provenance,
                evidence_ref=ArtifactId(evidence[0].artifact_ref),
            ),
            "price_unit": SourcingObservedFact(
                value="piece",
                provenance=provenance,
                evidence_ref=ArtifactId(evidence[0].artifact_ref),
            ),
            "currency": SourcingObservedFact(
                value="USD",
                provenance=provenance,
                evidence_ref=ArtifactId(evidence[0].artifact_ref),
            ),
        }
    )
    return SupplierCandidate(
        candidate_id=candidate_id,
        tenant_id=tenant_id,
        case_id=case_id,
        supplier_name="Factory A",
        product_title="Stainless hinge",
        created_at=NOW,
        source_platform="official_site",
        observed_facts=facts,
        supplier_claims={name: claim for name in ("product_type", "material", "size", "model")},
        match_inferences={"substitution": inference},
        verified_specs=specs,
        indicative_price_tiers=(
            IndicativePriceTier(
                minimum_quantity=1000,
                amount=Decimal("0.123456789012"),
                currency="USD",
                unit="piece",
                provenance=provenance,
                evidence_ref=ArtifactId(evidence[0].artifact_ref),
            ),
        ),
        moq=1000,
        price_unit="piece",
        currency="USD",
        evidence=evidence[0],
        evidence_snapshots=evidence,
        match=MatchExplanation(MatchLadderRung.PUBLIC_SOURCING, specs, "全部匹配"),
        rejected=False,
        rejection_reasons=[],
        verified_by=EmployeeId("emp-verifier"),
    )


async def test_sourcing_aggregate_round_trips_with_stable_evidence_order(
    sourcing_engine: AsyncEngine,
) -> None:
    """删除任一实体映射、金额精度或证据排序都会让聚合往返失败。"""

    SqlAlchemySourcingUnitOfWork = _symbol(
        "infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork"
    )
    LadderCheck = _symbol("domains.sourcing.models", "LadderCheck")
    LadderOutcome = _symbol("domains.sourcing.models", "LadderOutcome")
    SearchExecution = _symbol("domains.sourcing.models", "SourcingSearchExecution")
    SearchExecutionStatus = _symbol(
        "domains.sourcing.models", "SourcingSearchExecutionStatus"
    )
    SearchReconciliation = _symbol(
        "domains.sourcing.models", "SourcingSearchReconciliation"
    )
    SourcingReview = _symbol("domains.sourcing.models", "SourcingReview")
    ReconciliationStatus = _symbol(
        "domains.sourcing.models", "SourcingReconciliationStatus"
    )

    tenant_id, need_id, case_id = _tenant(), ValidatedNeedId(new_id("need")), SourcingCaseId(new_id("src"))
    artifact_late, artifact_early = _artifact(), _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    await _seed_artifact(sourcing_engine, tenant_id, artifact_late, content_hash="b" * 64)
    await _seed_artifact(sourcing_engine, tenant_id, artifact_early, content_hash="c" * 64)
    case = _case(tenant_id, need_id, case_id, artifact_early)
    plan = _plan(tenant_id, case_id)
    evidence_late = EvidenceSnapshot(
        "https://factory.example/late", NOW + timedelta(minutes=1), "b" * 64, str(artifact_late)
    )
    evidence_early = EvidenceSnapshot(
        "https://factory.example/early", NOW, "c" * 64, str(artifact_early)
    )
    candidate_id = SupplierCandidateId(new_id("spc"))
    candidate = _candidate(
        tenant_id, case_id, candidate_id, (evidence_late, evidence_early)
    )
    option = SourcingSupplyOption(
        option_id=SourcingSupplyOptionId(new_id("sop")),
        tenant_id=tenant_id,
        case_id=case_id,
        source=SupplyOptionSource.SUPPLIER_CANDIDATE,
        product_id=ProductId(new_id("prd")),
        supplier_candidate_id=candidate_id,
        is_qualified=True,
        created_at=NOW,
    )

    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO products (tenant_id, product_id, pool, candidate_status, "
                "name_zh, name_en, category, normalized_category, sellable_markets, "
                "customizable, selling_points, known_issues, created_at) VALUES "
                "(:tenant, :product, 'candidate', 'source_only', '铰链', 'Hinge', "
                "'hinges', 'hinges', '[]', false, '[]', '[]', :created_at)"
            ),
            {"tenant": tenant_id, "product": option.product_id, "created_at": NOW},
        )
        run_id = RunId(new_id("run"))
        await connection.execute(
            text(
                "INSERT INTO workflow_runs (run_id, tenant_id, workflow_type, workflow_version, "
                "subject_ref, current_step, status, context, idempotency_key) VALUES "
                "(:run, :tenant, 'sourcing_case_v2', 2, :case, 'public_search', "
                "'running', '{}', :key)"
            ),
            {"run": run_id, "tenant": tenant_id, "case": case_id, "key": str(run_id)},
        )

    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        await uow.cases.add(tenant_id, case)
        await uow.checks.add(
            tenant_id,
            LadderCheck(
                check_id=new_id("slc"), tenant_id=tenant_id, case_id=case_id,
                sequence_number=1, rung=MatchLadderRung.CATALOG_EXACT,
                outcome=LadderOutcome.NO_QUALIFIED_SUPPLY,
                input_snapshot={"category": "hinges"}, input_snapshot_hash="d" * 64,
                conclusion="无完全匹配", match_object_type=None, match_object_id=None,
                spec_comparisons=(), evidence_refs=(),
                checked_by=EmployeeId("emp-checker"), checked_at=NOW,
            ),
        )
        await uow.plans.add(tenant_id, plan)
        await uow.candidates.add(tenant_id, candidate)
        await uow.options.add(tenant_id, option)
        review = SourcingReview.create(
            review_id=SourcingReviewId(new_id("srv")),
            tenant_id=tenant_id,
            case_id=case_id,
            command=SourcingReviewCommand(
                primary_option_id=option.option_id,
                alternate_option_ids=(),
                reason="主候选证据完整",
                expected_case_version=1,
            ),
            submitted_by=EmployeeId("emp-reviewer"),
            submitted_at=NOW,
            actual_case_version=1,
        )
        await uow.reviews.add(tenant_id, review)
        execution = SearchExecution(
            execution_id=new_id("sex"), tenant_id=tenant_id, case_id=case_id,
            plan_id=plan.plan_id, run_id=run_id, plan_hash=plan.plan_hash,
            query_index=0, request_key="e" * 64, query_text="hinge manufacturer",
            locator_results=({"url": "https://factory.example"},),
            provider_status=SearchExecutionStatus.SUCCEEDED,
            created_at=NOW, completed_at=NOW,
        )
        await uow.search_executions.add(tenant_id, execution)
        reconciliation = SearchReconciliation(
            reconciliation_id=new_id("srr"), tenant_id=tenant_id,
            execution_id=execution.execution_id,
            status=ReconciliationStatus.REQUIRED, reason="人工核对提供商账单",
            provider_receipt={}, created_at=NOW,
        )
        await uow.reconciliations.add(tenant_id, reconciliation)

    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        loaded_case = await uow.cases.get(tenant_id, case_id)
        loaded_checks = await uow.checks.list_for_case(tenant_id, case_id)
        loaded_plan = await uow.plans.get(tenant_id, plan.plan_id)
        loaded_candidate = await uow.candidates.get(tenant_id, candidate_id)
        loaded_options = await uow.options.list_for_case(tenant_id, case_id)
        loaded_review = await uow.reviews.get_for_case(tenant_id, case_id)
        loaded_execution = await uow.search_executions.get_by_request_key(
            tenant_id, "e" * 64
        )
        loaded_reconciliation = await uow.reconciliations.get_for_execution(
            tenant_id, execution.execution_id
        )

    assert loaded_case == case
    assert loaded_case.stop_detail == SourcingStopDetail(stage=SourcingStopStage.PROVIDER)
    assert loaded_checks[0].conclusion == "无完全匹配"
    assert loaded_plan == plan
    assert loaded_candidate is not None
    assert loaded_candidate.indicative_price_tiers[0].amount == Decimal("0.123456789012")
    assert (
        loaded_candidate.indicative_price_tiers[0].provenance.source_id
        == "page-field"
    )
    assert loaded_candidate.indicative_price_tiers[0].evidence_ref == artifact_late
    assert [item.artifact_ref for item in loaded_candidate.evidence_snapshots] == [
        str(artifact_early), str(artifact_late)
    ]
    assert loaded_options == [option]
    assert loaded_review == review
    assert loaded_execution == execution
    assert loaded_reconciliation == reconciliation


async def test_case_and_review_cas_reject_stale_writes_without_overwrite(
    sourcing_engine: AsyncEngine,
) -> None:
    """去掉 tenant+case+version 条件会让旧状态或旧审核覆盖新事实。"""

    SqlAlchemySourcingUnitOfWork = _symbol(
        "infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork"
    )
    SourcingCaseConflictError = _symbol(
        "domains.sourcing.errors", "SourcingCaseConflictError"
    )
    SourcingReview = _symbol("domains.sourcing.models", "SourcingReview")
    tenant_id, need_id, case_id = _tenant(), ValidatedNeedId(new_id("need")), SourcingCaseId(new_id("src"))
    artifact_id = _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    case = _case(tenant_id, need_id, case_id, artifact_id)
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        await uow.cases.add(tenant_id, case)
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        fresh = await uow.cases.get(tenant_id, case_id)
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        stale = await uow.cases.get(tenant_id, case_id)
    assert fresh is not None and stale is not None
    fresh.transition_to(CaseState.DISCOVERING, changed_at=NOW + timedelta(minutes=1))
    stale.transition_to(CaseState.FAILED, changed_at=NOW + timedelta(minutes=2))
    stale.failed_reason = "stale failure"
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        await uow.cases.update(tenant_id, fresh)
    with pytest.raises(SourcingCaseConflictError):
        async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
            await uow.cases.update(tenant_id, stale)
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        current = await uow.cases.get(tenant_id, case_id)
    assert current is not None
    assert current.state is CaseState.DISCOVERING
    assert current.failed_reason is None

    review = SourcingReview.create(
        review_id=SourcingReviewId(new_id("srv")), tenant_id=tenant_id,
        case_id=case_id,
        command=SourcingReviewCommand(
            primary_option_id=SourcingSupplyOptionId(new_id("sop")),
            alternate_option_ids=(), reason="旧版本审核", expected_case_version=1,
        ),
        submitted_by=EmployeeId("emp-reviewer"), submitted_at=NOW,
        actual_case_version=1,
    )
    with pytest.raises(SourcingCaseConflictError):
        async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
            await uow.reviews.add(tenant_id, review)
    async with sourcing_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM sourcing_reviews WHERE tenant_id=:tenant AND case_id=:case"),
            {"tenant": tenant_id, "case": case_id},
        )
    assert count == 0


async def test_public_plan_confirmation_cas_rejects_a_second_stale_confirmation(
    sourcing_engine: AsyncEngine,
) -> None:
    """若持久更新不检查前态与精确哈希，两个旧确认都会被当作成功。"""

    Uow = _symbol("infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork")
    Conflict = _symbol("domains.sourcing.errors", "SourcingCaseConflictError")
    tenant_id = _tenant()
    need_id = ValidatedNeedId(new_id("need"))
    case_id = SourcingCaseId(new_id("src"))
    artifact_id = _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    plan = _plan(tenant_id, case_id)
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    async with Uow(sf, tenant_id) as uow:
        await uow.cases.add(tenant_id, _case(tenant_id, need_id, case_id, artifact_id))
        await uow.plans.add(tenant_id, plan)
    async with Uow(sf, tenant_id) as uow:
        first = await uow.plans.get(tenant_id, plan.plan_id)
    async with Uow(sf, tenant_id) as uow:
        stale = await uow.plans.get(tenant_id, plan.plan_id)
    assert first is not None and stale is not None
    first = first.confirm(EmployeeId("emp-boss-a"), confirmed_at=NOW)
    stale = stale.confirm(EmployeeId("emp-boss-b"), confirmed_at=NOW)
    async with Uow(sf, tenant_id) as uow:
        await uow.plans.update(tenant_id, first)
    with pytest.raises(Conflict):
        async with Uow(sf, tenant_id) as uow:
            await uow.plans.update(tenant_id, stale)


async def test_public_plan_confirmation_rejects_changed_case_atomically(
    sourcing_engine: AsyncEngine,
) -> None:
    """计划确认必须在同事务锁定并核对其绑定的 Case 版本。"""

    Uow = _symbol("infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork")
    Conflict = _symbol("domains.sourcing.errors", "SourcingCaseConflictError")
    tenant_id, need_id = _tenant(), ValidatedNeedId(new_id("need"))
    case_id, artifact_id = SourcingCaseId(new_id("src")), _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    plan = _plan(tenant_id, case_id)
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    async with Uow(sf, tenant_id) as uow:
        await uow.cases.add(
            tenant_id, _case(tenant_id, need_id, case_id, artifact_id)
        )
        await uow.plans.add(tenant_id, plan)
    async with Uow(sf, tenant_id) as uow:
        changed_case = await uow.cases.get(tenant_id, case_id)
        pending_plan = await uow.plans.get(tenant_id, plan.plan_id)
    assert changed_case is not None and pending_plan is not None
    changed_case.transition_to(
        CaseState.DISCOVERING, changed_at=NOW + timedelta(minutes=1)
    )
    async with Uow(sf, tenant_id) as uow:
        await uow.cases.update(tenant_id, changed_case)
    authorized = pending_plan.confirm(EmployeeId("emp-boss"), confirmed_at=NOW)

    with pytest.raises(Conflict, match="案例版本"):
        async with Uow(sf, tenant_id) as uow:
            await uow.plans.update(tenant_id, authorized)

    async with Uow(sf, tenant_id) as uow:
        stored = await uow.plans.get(tenant_id, plan.plan_id)
    assert stored is not None
    assert stored.status.value == "pending_confirmation"
    assert stored.confirmed_by is None


async def test_review_confirmation_cas_preserves_first_confirmer(
    sourcing_engine: AsyncEngine,
) -> None:
    """两个基于未确认前态的确认中，第二个必须冲突且不能覆盖第一人。"""

    Uow = _symbol("infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork")
    Conflict = _symbol("domains.sourcing.errors", "SourcingCaseConflictError")
    Review = _symbol("domains.sourcing.models", "SourcingReview")
    tenant_id, need_id = _tenant(), ValidatedNeedId(new_id("need"))
    case_id, artifact_id = SourcingCaseId(new_id("src")), _artifact()
    product_id, option_id = ProductId(new_id("prd")), SourcingSupplyOptionId(
        new_id("sop")
    )
    review_id = SourcingReviewId(new_id("srv"))
    await _seed_need(sourcing_engine, tenant_id, need_id)
    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO products "
                "(tenant_id, product_id, pool, name_zh, name_en, category, "
                "normalized_category, sellable_markets, customizable, selling_points, "
                "known_issues, created_at) VALUES "
                "(:tenant, :product, 'formal', '铰链', 'Hinge', 'hinges', 'hinges', "
                "'[]', false, '[]', '[]', :now)"
            ),
            {"tenant": tenant_id, "product": product_id, "now": NOW},
        )
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    review = Review.create(
        review_id=review_id,
        tenant_id=tenant_id,
        case_id=case_id,
        command=SourcingReviewCommand(
            primary_option_id=option_id,
            alternate_option_ids=(),
            reason="主选",
            expected_case_version=1,
        ),
        submitted_by=EmployeeId("emp-reviewer"),
        submitted_at=NOW,
        actual_case_version=1,
    )
    async with Uow(sf, tenant_id) as uow:
        await uow.cases.add(
            tenant_id, _case(tenant_id, need_id, case_id, artifact_id)
        )
        await uow.options.add(
            tenant_id,
            SourcingSupplyOption(
                option_id=option_id,
                tenant_id=tenant_id,
                case_id=case_id,
                source=SupplyOptionSource.EXISTING_PRODUCT,
                product_id=product_id,
                supplier_candidate_id=None,
                is_qualified=True,
                created_at=NOW,
            ),
        )
        await uow.reviews.add(tenant_id, review)
    async with Uow(sf, tenant_id) as uow:
        first = await uow.reviews.get(tenant_id, review_id)
    async with Uow(sf, tenant_id) as uow:
        stale = await uow.reviews.get(tenant_id, review_id)
    assert first is not None and stale is not None
    first = first.confirm(EmployeeId("emp-boss-a"), confirmed_at=NOW)
    stale = stale.confirm(EmployeeId("emp-boss-b"), confirmed_at=NOW)
    async with Uow(sf, tenant_id) as uow:
        await uow.reviews.update(tenant_id, first)
    with pytest.raises(Conflict):
        async with Uow(sf, tenant_id) as uow:
            await uow.reviews.update(tenant_id, stale)
    async with Uow(sf, tenant_id) as uow:
        stored = await uow.reviews.get(tenant_id, review_id)
    assert stored is not None
    assert stored.confirmed_by == EmployeeId("emp-boss-a")


async def test_handoff_snapshot_is_tenant_bound_confirmed_primary_and_complete(
    sourcing_engine: AsyncEngine,
) -> None:
    """交接只从绑定机会、已确认主选和完整逐档证据构建，不补造字段。"""

    Uow = _symbol("infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork")
    Review = _symbol("domains.sourcing.models", "SourcingReview")
    tenant_id, other_tenant = _tenant(), _tenant()
    need_id, case_id = ValidatedNeedId(new_id("need")), SourcingCaseId(
        new_id("src")
    )
    opportunity_id = OpportunityId(new_id("opp"))
    candidate_id = SupplierCandidateId(new_id("spc"))
    product_id, alternate_product_id = ProductId(new_id("prd")), ProductId(
        new_id("prd")
    )
    option_id, alternate_option_id = SourcingSupplyOptionId(
        new_id("sop")
    ), SourcingSupplyOptionId(new_id("sop"))
    review_id = SourcingReviewId(new_id("srv"))
    artifact_id = _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    await _seed_artifact(
        sourcing_engine, tenant_id, artifact_id, content_hash="d" * 64
    )
    await _seed_opportunity(
        sourcing_engine, tenant_id, opportunity_id, need_id
    )
    evidence = EvidenceSnapshot(
        "https://factory.example/product",
        NOW,
        "d" * 64,
        str(artifact_id),
    )
    candidate = _candidate(
        tenant_id, case_id, candidate_id, (evidence,)
    )
    case = _case(tenant_id, need_id, case_id, artifact_id)
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    async with Uow(sf, tenant_id) as uow:
        await uow.cases.add(tenant_id, case)
        await uow.candidates.add(tenant_id, candidate)
    await _advance_case_to_candidates_ready(Uow, sf, tenant_id, case_id)
    async with sourcing_engine.begin() as connection:
        for current_product in (product_id, alternate_product_id):
            await connection.execute(
                text(
                    "INSERT INTO products "
                    "(tenant_id, product_id, pool, candidate_status, name_zh, name_en, "
                    "category, normalized_category, moq, sellable_markets, customizable, "
                    "selling_points, known_issues, created_at) VALUES "
                    "(:tenant, :product, 'candidate', 'source_only', '铰链', 'Hinge', "
                    "'hinges', 'hinges', 1000, '[]', false, '[]', '[]', :now)"
                ),
                {"tenant": tenant_id, "product": current_product, "now": NOW},
            )
        await connection.execute(
            text(
                "INSERT INTO product_candidate_sources "
                "(tenant_id, product_id, sourcing_case_id, supplier_candidate_id, created_at) "
                "VALUES (:tenant, :product, :case, :candidate, :now)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "case": case_id,
                "candidate": candidate_id,
                "now": NOW,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO product_candidate_price_refs "
                "(tenant_id, product_id, minimum_quantity, unit_amount, currency, unit, artifact_id) "
                "VALUES (:tenant, :product, 1000, :amount, 'USD', 'piece', :artifact)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "amount": Decimal("0.123456789012"),
                "artifact": artifact_id,
            },
        )
    review = Review.create(
        review_id=review_id,
        tenant_id=tenant_id,
        case_id=case_id,
        command=SourcingReviewCommand(
            primary_option_id=option_id,
            alternate_option_ids=(alternate_option_id,),
            reason="主候选证据完整",
            expected_case_version=4,
        ),
        submitted_by=EmployeeId("emp-reviewer"),
        submitted_at=NOW,
        actual_case_version=4,
    )
    async with Uow(sf, tenant_id) as uow:
        await uow.options.add(
            tenant_id,
            SourcingSupplyOption(
                option_id=option_id,
                tenant_id=tenant_id,
                case_id=case_id,
                source=SupplyOptionSource.SUPPLIER_CANDIDATE,
                product_id=product_id,
                supplier_candidate_id=candidate_id,
                is_qualified=True,
                created_at=NOW,
            ),
        )
        await uow.options.add(
            tenant_id,
            SourcingSupplyOption(
                option_id=alternate_option_id,
                tenant_id=tenant_id,
                case_id=case_id,
                source=SupplyOptionSource.EXISTING_PRODUCT,
                product_id=alternate_product_id,
                supplier_candidate_id=None,
                is_qualified=True,
                created_at=NOW,
            ),
        )
        await uow.reviews.add(tenant_id, review)
    async with Uow(sf, tenant_id) as uow:
        assert await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        ) is None
    confirmed = review.confirm(EmployeeId("emp-boss"), confirmed_at=NOW)
    async with Uow(sf, tenant_id) as uow:
        await uow.reviews.update(tenant_id, confirmed)
    async with Uow(sf, tenant_id) as uow:
        assert await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        ) is None
    async with Uow(sf, tenant_id) as uow:
        current_case = await uow.cases.get(tenant_id, case_id)
        assert current_case is not None
        current_case.opportunity_id = opportunity_id
        current_case.transition_to(
            CaseState.HANDED_TO_COSTING,
            changed_at=NOW + timedelta(seconds=4),
        )
        current_case.completed_at = NOW + timedelta(seconds=4)
        await uow.cases.update(tenant_id, current_case)
    async with Uow(sf, tenant_id) as uow:
        snapshot = await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        )
    assert snapshot is not None
    assert snapshot.case_id == case_id
    assert snapshot.review_id == review_id
    assert snapshot.opportunity_id == opportunity_id
    assert snapshot.primary_option_id == option_id
    assert snapshot.product_id == product_id
    assert snapshot.supplier_candidate_id == candidate_id
    assert snapshot.quantity == 5000
    assert snapshot.moq == 1000
    assert len(snapshot.price_options) == 1
    assert snapshot.price_options[0].unit_amount == Decimal("0.123456789012")
    assert snapshot.price_options[0].unit == "piece"
    assert snapshot.price_options[0].evidence_ref == artifact_id
    async with Uow(sf, other_tenant) as uow:
        assert await uow.handoffs.get_snapshot(
            other_tenant, case_id, review_id
        ) is None

    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sourcing_cases SET opportunity_id = NULL "
                "WHERE tenant_id = :tenant AND case_id = :case"
            ),
            {"tenant": tenant_id, "case": case_id},
        )
    async with Uow(sf, tenant_id) as uow:
        assert await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        ) is None

    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sourcing_cases SET opportunity_id = :opportunity "
                "WHERE tenant_id = :tenant AND case_id = :case"
            ),
            {
                "tenant": tenant_id,
                "case": case_id,
                "opportunity": opportunity_id,
            },
        )
        await connection.execute(
            text(
                "DELETE FROM product_candidate_price_refs "
                "WHERE tenant_id = :tenant AND product_id = :product"
            ),
            {"tenant": tenant_id, "product": product_id},
        )
    async with Uow(sf, tenant_id) as uow:
        assert await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        ) is None

    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO product_candidate_price_refs "
                "(tenant_id, product_id, minimum_quantity, unit_amount, currency, unit, artifact_id) "
                "VALUES (:tenant, :product, 1000, :amount, 'USD', 'piece', :artifact)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "amount": Decimal("0.123456789012"),
                "artifact": artifact_id,
            },
        )
        await connection.execute(
            text(
                "UPDATE sourcing_candidates SET moq = NULL "
                "WHERE tenant_id = :tenant AND candidate_id = :candidate"
            ),
            {"tenant": tenant_id, "candidate": candidate_id},
        )
    async with Uow(sf, tenant_id) as uow:
        assert await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        ) is None

    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sourcing_candidates SET moq = 1000 "
                "WHERE tenant_id = :tenant AND candidate_id = :candidate"
            ),
            {"tenant": tenant_id, "candidate": candidate_id},
        )
        await connection.execute(
            text(
                "UPDATE sourcing_cases SET need_snapshot = need_snapshot - 'quantity' "
                "WHERE tenant_id = :tenant AND case_id = :case"
            ),
            {"tenant": tenant_id, "case": case_id},
        )
    async with Uow(sf, tenant_id) as uow:
        assert await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        ) is None


async def test_existing_product_handoff_preserves_cost_unit_and_evidence(
    sourcing_engine: AsyncEngine,
) -> None:
    """现有产品只有五项成本来源完整时才生成单一 indicative 价格档。"""

    Uow = _symbol("infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork")
    Review = _symbol("domains.sourcing.models", "SourcingReview")
    tenant_id, need_id = _tenant(), ValidatedNeedId(new_id("need"))
    case_id, opportunity_id = SourcingCaseId(new_id("src")), OpportunityId(
        new_id("opp")
    )
    product_id, option_id = ProductId(new_id("prd")), SourcingSupplyOptionId(
        new_id("sop")
    )
    review_id, artifact_id = SourcingReviewId(new_id("srv")), _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    await _seed_artifact(
        sourcing_engine, tenant_id, artifact_id, content_hash="e" * 64
    )
    await _seed_opportunity(
        sourcing_engine, tenant_id, opportunity_id, need_id
    )
    case = _case(tenant_id, need_id, case_id, artifact_id)
    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO products "
                "(tenant_id, product_id, pool, name_zh, name_en, category, "
                "normalized_category, moq, internal_cost_amount, internal_cost_currency, "
                "internal_cost_basis, internal_cost_unit, internal_cost_source_ref, "
                "sellable_markets, customizable, selling_points, known_issues, created_at) "
                "VALUES (:tenant, :product, 'formal', '铰链', 'Hinge', 'hinges', "
                "'hinges', 500, :amount, 'USD', 'supplier_quote', 'piece', :artifact, "
                "'[]', false, '[]', '[]', :now)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "amount": Decimal("0.222222222222"),
                "artifact": artifact_id,
                "now": NOW,
            },
        )
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    async with Uow(sf, tenant_id) as uow:
        await uow.cases.add(tenant_id, case)
        await uow.options.add(
            tenant_id,
            SourcingSupplyOption(
                option_id=option_id,
                tenant_id=tenant_id,
                case_id=case_id,
                source=SupplyOptionSource.EXISTING_PRODUCT,
                product_id=product_id,
                supplier_candidate_id=None,
                is_qualified=True,
                created_at=NOW,
            ),
        )
    await _advance_case_to_candidates_ready(Uow, sf, tenant_id, case_id)
    review = Review.create(
        review_id=review_id,
        tenant_id=tenant_id,
        case_id=case_id,
        command=SourcingReviewCommand(
            primary_option_id=option_id,
            alternate_option_ids=(),
            reason="现货成本资料完整",
            expected_case_version=4,
        ),
        submitted_by=EmployeeId("emp-reviewer"),
        submitted_at=NOW,
        actual_case_version=4,
    )
    async with Uow(sf, tenant_id) as uow:
        await uow.reviews.add(tenant_id, review)
    async with Uow(sf, tenant_id) as uow:
        await uow.reviews.update(
            tenant_id,
            review.confirm(EmployeeId("emp-boss"), confirmed_at=NOW),
        )
    async with Uow(sf, tenant_id) as uow:
        current_case = await uow.cases.get(tenant_id, case_id)
        assert current_case is not None
        current_case.opportunity_id = opportunity_id
        current_case.transition_to(
            CaseState.HANDED_TO_COSTING,
            changed_at=NOW + timedelta(seconds=4),
        )
        current_case.completed_at = NOW + timedelta(seconds=4)
        await uow.cases.update(tenant_id, current_case)
    async with Uow(sf, tenant_id) as uow:
        snapshot = await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        )
    assert snapshot is not None
    assert snapshot.supplier_candidate_id is None
    assert snapshot.quantity == 5000
    assert snapshot.moq == 500
    assert snapshot.price_options[0].minimum_quantity == 500
    assert snapshot.price_options[0].unit_amount == Decimal("0.222222222222")
    assert snapshot.price_options[0].unit == "piece"
    assert snapshot.price_options[0].evidence_ref == artifact_id
    assert snapshot.price_options[0].source_kind == "existing_product"


@pytest.mark.parametrize(
    ("case_state", "case_version"),
    (
        (CaseState.OPENED, 4),
        (CaseState.CANDIDATES_READY, 4),
        (CaseState.HANDED_TO_COSTING, 4),
    ),
    ids=("opened", "candidates-ready", "wrong-handoff-version"),
)
async def test_handoff_snapshot_requires_completed_transition_and_next_version(
    sourcing_engine: AsyncEngine,
    case_state: Any,
    case_version: int,
) -> None:
    """缺少 handed_to_costing 状态或 expected+1 版本时不得读取交接。"""

    Uow = _symbol("infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork")
    Review = _symbol("domains.sourcing.models", "SourcingReview")
    tenant_id, need_id = _tenant(), ValidatedNeedId(new_id("need"))
    case_id, opportunity_id = SourcingCaseId(new_id("src")), OpportunityId(
        new_id("opp")
    )
    product_id, option_id = ProductId(new_id("prd")), SourcingSupplyOptionId(
        new_id("sop")
    )
    review_id, artifact_id = SourcingReviewId(new_id("srv")), _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    await _seed_artifact(
        sourcing_engine, tenant_id, artifact_id, content_hash="f" * 64
    )
    await _seed_opportunity(
        sourcing_engine, tenant_id, opportunity_id, need_id
    )
    async with sourcing_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO products "
                "(tenant_id, product_id, pool, name_zh, name_en, category, "
                "normalized_category, moq, internal_cost_amount, internal_cost_currency, "
                "internal_cost_basis, internal_cost_unit, internal_cost_source_ref, "
                "sellable_markets, customizable, selling_points, known_issues, created_at) "
                "VALUES (:tenant, :product, 'formal', '铰链', 'Hinge', 'hinges', "
                "'hinges', 500, :amount, 'USD', 'supplier_quote', 'piece', :artifact, "
                "'[]', false, '[]', '[]', :now)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "amount": Decimal("0.333333333333"),
                "artifact": artifact_id,
                "now": NOW,
            },
        )
    case = _case(tenant_id, need_id, case_id, artifact_id)
    case.opportunity_id = opportunity_id
    case.state = case_state
    case.version = case_version
    case.completed_at = NOW if case_state is CaseState.HANDED_TO_COSTING else None
    review = Review.create(
        review_id=review_id,
        tenant_id=tenant_id,
        case_id=case_id,
        command=SourcingReviewCommand(
            primary_option_id=option_id,
            alternate_option_ids=(),
            reason="生命周期门禁",
            expected_case_version=4,
        ),
        submitted_by=EmployeeId("emp-reviewer"),
        submitted_at=NOW,
        actual_case_version=4,
    ).confirm(EmployeeId("emp-boss"), confirmed_at=NOW)
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    async with Uow(sf, tenant_id) as uow:
        await uow.cases.add(tenant_id, case)
        await uow.options.add(
            tenant_id,
            SourcingSupplyOption(
                option_id=option_id,
                tenant_id=tenant_id,
                case_id=case_id,
                source=SupplyOptionSource.EXISTING_PRODUCT,
                product_id=product_id,
                supplier_candidate_id=None,
                is_qualified=True,
                created_at=NOW,
            ),
        )
        await uow.reviews.add(tenant_id, review)
    async with Uow(sf, tenant_id) as uow:
        assert await uow.handoffs.get_snapshot(
            tenant_id, case_id, review_id
        ) is None


async def test_sourcing_tenant_mismatch_fails_before_query_and_bound_lookup_isolated(
    sourcing_engine: AsyncEngine,
) -> None:
    """移除显式 tenant 检查或 SQL tenant 谓词会泄漏同 ID 数据。"""

    Repo = _symbol("infra.db.repositories.sourcing", "SourcingCaseRepositoryImpl")
    tenant_a, tenant_b = _tenant(), _tenant()
    need_id, case_id, artifact_id = ValidatedNeedId(new_id("need")), SourcingCaseId(new_id("src")), _artifact()
    await _seed_need(sourcing_engine, tenant_a, need_id)
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    session_a = sf()
    session_b = sf()
    try:
        repo_a, repo_b = Repo(session_a, tenant_a), Repo(session_b, tenant_b)
        await repo_a.add(tenant_a, _case(tenant_a, need_id, case_id, artifact_id))
        await session_a.commit()
        assert await repo_b.get(tenant_b, case_id) is None
        with pytest.raises(ValueError, match="租户"):
            await repo_b.get(tenant_a, case_id)
        with pytest.raises(ValueError, match="租户"):
            await repo_b.add(tenant_b, _case(tenant_a, need_id, case_id, artifact_id))
    finally:
        await session_a.close()
        await session_b.close()


async def test_case_repository_rejects_snapshot_hash_mismatch_before_insert(
    sourcing_engine: AsyncEngine,
) -> None:
    """若实体哈希可与快照正文分离，后续审计无法证明冻结的是哪份 Need。"""

    Repo = _symbol("infra.db.repositories.sourcing", "SourcingCaseRepositoryImpl")
    tenant_id = _tenant()
    need_id = ValidatedNeedId(new_id("need"))
    case_id = SourcingCaseId(new_id("src"))
    artifact_id = _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    case = _case(tenant_id, need_id, case_id, artifact_id)
    case.need_snapshot_hash = "b" * 64
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)
    session = sf()
    try:
        with pytest.raises(ValidationError, match="快照哈希"):
            await Repo(session, tenant_id).add(tenant_id, case)
        await session.rollback()
    finally:
        await session.close()


@pytest.mark.parametrize("raise_after_publish", [False, True])
async def test_sourcing_uow_commits_or_rolls_back_business_and_outbox_together(
    sourcing_engine: AsyncEngine,
    raise_after_publish: bool,
) -> None:
    """拆分 bus session 或错误提交异常路径会留下孤立业务行/事件。"""

    SqlAlchemySourcingUnitOfWork = _symbol(
        "infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork"
    )
    tenant_id, need_id, case_id = _tenant(), ValidatedNeedId(new_id("need")), SourcingCaseId(new_id("src"))
    artifact_id = _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    sf = async_sessionmaker(sourcing_engine, expire_on_commit=False)

    async def execute() -> None:
        async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
            await uow.cases.add(tenant_id, _case(tenant_id, need_id, case_id, artifact_id))
            await uow.bus.publish(
                SourcingCandidatesReady(
                    tenant_id=tenant_id, occurred_at=NOW, run_id=None,
                    case_id=case_id, option_ids=(), candidate_ids=(),
                )
            )
            if raise_after_publish:
                raise RuntimeError("force rollback")

    if raise_after_publish:
        with pytest.raises(RuntimeError, match="force rollback"):
            await execute()
    else:
        await execute()
    async with sourcing_engine.connect() as connection:
        business_count = await connection.scalar(
            select(func.count()).select_from(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
        outbox_count = await connection.scalar(
            select(func.count()).select_from(OutboxEventRow).where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCandidatesReady",
            )
        )
    expected = 0 if raise_after_publish else 1
    assert business_count == expected
    assert outbox_count == expected


async def test_sourcing_uow_commit_failure_rolls_back_business_and_outbox(
    sourcing_engine: AsyncEngine,
) -> None:
    """commit 本身失败也不能留下已 flush 的业务事实或 outbox。"""

    Uow = _symbol("infra.db.sourcing_uow", "SqlAlchemySourcingUnitOfWork")
    tenant_id, need_id = _tenant(), ValidatedNeedId(new_id("need"))
    case_id, artifact_id = SourcingCaseId(new_id("src")), _artifact()
    await _seed_need(sourcing_engine, tenant_id, need_id)
    sf = async_sessionmaker(
        sourcing_engine,
        class_=_CommitFailingSession,
        expire_on_commit=False,
    )
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        async with Uow(sf, tenant_id) as uow:
            await uow.cases.add(
                tenant_id, _case(tenant_id, need_id, case_id, artifact_id)
            )
            await uow.bus.publish(
                SourcingCandidatesReady(
                    tenant_id=tenant_id,
                    occurred_at=NOW,
                    run_id=None,
                    case_id=case_id,
                    option_ids=(),
                    candidate_ids=(),
                )
            )
    async with sourcing_engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        ) == 0
        assert await connection.scalar(
            select(func.count()).select_from(OutboxEventRow).where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCandidatesReady",
            )
        ) == 0
