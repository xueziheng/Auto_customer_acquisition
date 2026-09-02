"""SourcingServiceImpl 经真实 PostgreSQL UoW 的关键持久化与 Outbox 验证。"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.demand.schemas import NeedClusterPriorityFacts
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
    PublicCandidateDraft,
    PublicCandidateDraftPriceTier,
    PublicCandidateDraftSpec,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingAdmissionEnqueueCommand,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingPriorityFactsInput,
    SourcingReviewCommand,
    SpecComparisonView,
    VerifyPublicCandidateDraftsCommand,
)
from domains.sourcing.service import CandidateEvidenceSnapshot
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import (
    OutboxEventRow,
    SourcingAdmissionRow,
    SourcingCaseRow,
    SourcingLadderCheckRow,
    SourcingPrioritySnapshotRow,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.events.catalog import NeedValidated
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    NeedClusterId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

NOW = datetime(2026, 8, 30, 11, tzinfo=UTC)

_models = importlib.import_module("domains.sourcing.models")
AdmissionBlockedReason = _models.AdmissionBlockedReason
AdmissionState = _models.AdmissionState
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
    raise AssertionError("pytest.fail 必须终止执行")


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
    invalid = valid.__class__(**{**valid.__dict__, "input_snapshot": snapshot})

    with pytest.raises(ValidationError, match="证据映射"):
        await service.record_ladder_check(tenant_id, case_id, invalid, actor=system)

    async with sf() as session:
        case = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
        count = await session.scalar(
            select(func.count())
            .select_from(SourcingLadderCheckRow)
            .where(
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
            material=NeedFact(value="required-material", provenance=provenance),
            size_spec=NeedFact(value="required-size", provenance=provenance),
            quantity=NeedFact(value=5000, provenance=provenance),
            snapshot_hash="a" * 64,
        ),
        trigger_key=f"sourcing-case:v2:{tenant_id}:{need_id}",
    )


@pytest.mark.asyncio
async def test_admission_service_persists_canonical_snapshots_and_token_transitions(
    integration_engine: AsyncEngine,
) -> None:
    """真实 UoW 下重复事实只增一次，且陈旧 token 不能覆盖 current starting。"""

    tenant_id = TenantId(new_id("tn"))
    first_need = ValidatedNeedId(new_id("need"))
    second_need = ValidatedNeedId(new_id("need"))
    await _seed_need(integration_engine, tenant_id, first_need)
    await _seed_need(integration_engine, tenant_id, second_need)
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    current_time = [NOW]
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    boss = SourcingActor("emp-boss", tenant_id, SourcingScope.TENANT, "boss")
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sessions, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: current_time[0],
    )
    first_case = await service.open_case(
        tenant_id, _command(tenant_id, first_need), actor=system
    )
    second_case = await service.open_case(
        tenant_id, _command(tenant_id, second_need), actor=system
    )

    def facts(
        need_id: ValidatedNeedId, count: int, cluster: str | None
    ) -> SourcingPriorityFactsInput:
        return SourcingPriorityFactsInput(
            need_id=need_id,
            cluster_id=(NeedClusterId(cluster) if cluster is not None else None),
            cluster_member_count=count,
            facts_observed_at=current_time[0],
        )

    first_id = await service.enqueue_admission(
        tenant_id,
        first_case,
        first_need,
        ready_at=NOW - timedelta(days=2),
        command=SourcingAdmissionEnqueueCommand(facts=facts(first_need, 1, None)),
        actor=system,
    )
    first_replay = await service.enqueue_admission(
        tenant_id,
        first_case,
        first_need,
        ready_at=NOW - timedelta(days=2),
        command=SourcingAdmissionEnqueueCommand(facts=facts(first_need, 1, None)),
        actor=system,
    )
    second_id = await service.enqueue_admission(
        tenant_id,
        second_case,
        second_need,
        ready_at=NOW - timedelta(days=1),
        command=SourcingAdmissionEnqueueCommand(
            facts=facts(second_need, 8, "cluster-eight")
        ),
        actor=system,
    )
    initial_view = await service.get_admission(tenant_id, first_id, actor=boss)
    replay_view = await service.get_admission(tenant_id, first_replay, actor=boss)
    assert first_replay == first_id
    assert replay_view == initial_view

    current_time[0] = NOW + timedelta(minutes=1)
    changed_facts = facts(first_need, 3, "cluster-three")
    changed_snapshot = await service.refresh_admission(
        tenant_id,
        first_id,
        facts=changed_facts,
        refreshed_at=current_time[0],
        actor=system,
    )
    replay_snapshot = await service.refresh_admission(
        tenant_id,
        first_id,
        facts=changed_facts,
        refreshed_at=current_time[0] + timedelta(seconds=1),
        actor=system,
    )
    assert replay_snapshot == changed_snapshot

    current_time[0] = NOW + timedelta(minutes=2)
    claimed = await service.claim_admissions(
        tenant_id,
        limit=2,
        claim_token="claim-current",
        claim_expires_at=NOW + timedelta(minutes=10),
        actor=system,
    )
    assert [item.admission_id for item in claimed] == [second_id, first_id]
    with pytest.raises(InvalidStateTransition, match="claim token"):
        await service.complete_admission(
            tenant_id,
            second_id,
            claim_token="claim-stale",
            workflow_run_id=RunId(new_id("run")),
            admitted_by="system:sourcing",
            admitted_at=NOW + timedelta(minutes=3),
            actor=system,
        )
    run_id = RunId(new_id("run"))
    await service.complete_admission(
        tenant_id,
        second_id,
        claim_token="claim-current",
        workflow_run_id=run_id,
        admitted_by="system:sourcing",
        admitted_at=NOW + timedelta(minutes=3),
        actor=system,
    )
    with pytest.raises(InvalidStateTransition, match="claim token"):
        await service.block_admission(
            tenant_id,
            first_id,
            reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
            blocked_at=NOW + timedelta(minutes=3),
            claim_token="claim-stale",
            actor=system,
        )
    await service.block_admission(
        tenant_id,
        first_id,
        reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
        blocked_at=NOW + timedelta(minutes=3),
        claim_token="claim-current",
        actor=system,
    )
    assert (
        await service.refresh_admission(
            tenant_id,
            second_id,
            facts=facts(second_need, 9, "cluster-nine"),
            refreshed_at=NOW + timedelta(minutes=4),
            actor=system,
        )
        is None
    )

    admitted = await service.list_admissions(
        tenant_id,
        state=AdmissionState.ADMITTED,
        limit=20,
        now=NOW + timedelta(minutes=4),
        actor=boss,
    )
    blocked = await service.list_admissions(
        tenant_id,
        state=AdmissionState.BLOCKED,
        limit=20,
        now=NOW + timedelta(minutes=4),
        actor=boss,
    )
    async with sessions() as session:
        admission_count = await session.scalar(
            select(func.count())
            .select_from(SourcingAdmissionRow)
            .where(SourcingAdmissionRow.tenant_id == str(tenant_id))
        )
        snapshot_count = await session.scalar(
            select(func.count())
            .select_from(SourcingPrioritySnapshotRow)
            .where(SourcingPrioritySnapshotRow.tenant_id == str(tenant_id))
        )
        stored_run = await session.scalar(
            select(SourcingAdmissionRow.workflow_run_id).where(
                SourcingAdmissionRow.tenant_id == str(tenant_id),
                SourcingAdmissionRow.admission_id == str(second_id),
            )
        )

    assert admission_count == 2
    assert snapshot_count == 3
    assert stored_run == str(run_id)
    assert [item.admission_id for item in admitted] == [second_id]
    assert admitted[0].admitted_by == "system:sourcing"
    assert [item.admission_id for item in blocked] == [first_id]
    assert blocked[0].blocked_reason == "case_state_mismatch"
    assert blocked[0].snapshot_id == changed_snapshot


@pytest.mark.asyncio
async def test_cluster_refresh_targets_are_unbounded_tenant_bound_and_idempotent_in_postgres(
    integration_engine: AsyncEngine,
) -> None:
    """真实 LEFT JOIN 同时覆盖整簇与 changed 无快照项，不截断或跨租户。"""

    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    cluster_id = NeedClusterId(new_id("ncl"))
    other_cluster = NeedClusterId(new_id("ncl"))
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant, SourcingScope.SYSTEM, "system")
    other_system = SourcingActor(
        "system-worker", other_tenant, SourcingScope.SYSTEM, "system"
    )
    service = _service_type()(
        lambda bound: SqlAlchemySourcingUnitOfWork(sessions, bound),
        Phase2SourcingAuthorizer(tenant),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    other_service = _service_type()(
        lambda bound: SqlAlchemySourcingUnitOfWork(sessions, bound),
        Phase2SourcingAuthorizer(other_tenant),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )

    async def enqueue(
        bound_service: Any,
        bound_tenant: TenantId,
        actor: SourcingActor,
        need_id: ValidatedNeedId,
        *,
        target_cluster: NeedClusterId | None,
        blocked: bool = False,
        member_count: int = 2,
    ) -> SourcingAdmissionId:
        await _seed_need(integration_engine, bound_tenant, need_id)
        case_id = await bound_service.open_case(
            bound_tenant, _command(bound_tenant, need_id), actor=actor
        )
        command = (
            SourcingAdmissionEnqueueCommand(
                facts=None, blocked_reason="priority_facts_invalid"
            )
            if blocked
            else SourcingAdmissionEnqueueCommand(
                facts=SourcingPriorityFactsInput(
                    need_id=need_id,
                    cluster_id=target_cluster,
                    cluster_member_count=(
                        member_count if target_cluster is not None else 1
                    ),
                    facts_observed_at=NOW - timedelta(minutes=1),
                )
            )
        )
        return await bound_service.enqueue_admission(
            bound_tenant,
            case_id,
            need_id,
            ready_at=NOW - timedelta(hours=1),
            command=command,
            actor=actor,
        )

    first_need, second_need, changed_need, starting_need, admitted_need = (
        ValidatedNeedId(new_id("need")) for _ in range(5)
    )
    other_cluster_need = ValidatedNeedId(new_id("need"))
    cross_tenant_need = ValidatedNeedId(new_id("need"))
    first_id = await enqueue(
        service, tenant, system, first_need, target_cluster=cluster_id
    )
    second_id = await enqueue(
        service, tenant, system, second_need, target_cluster=cluster_id
    )
    changed_id = await enqueue(
        service,
        tenant,
        system,
        changed_need,
        target_cluster=None,
        blocked=True,
    )
    starting_id = await enqueue(
        service,
        tenant,
        system,
        starting_need,
        target_cluster=cluster_id,
        member_count=20,
    )
    admitted_id = await enqueue(
        service,
        tenant,
        system,
        admitted_need,
        target_cluster=cluster_id,
        member_count=20,
    )
    other_cluster_id = await enqueue(
        service,
        tenant,
        system,
        other_cluster_need,
        target_cluster=other_cluster,
    )
    cross_tenant_id = await enqueue(
        other_service,
        other_tenant,
        other_system,
        cross_tenant_need,
        target_cluster=cluster_id,
    )
    claimed = await service.claim_admissions(
        tenant,
        limit=2,
        claim_token="claim-cluster-refresh",
        claim_expires_at=NOW + timedelta(minutes=10),
        actor=system,
    )
    assert {item.admission_id for item in claimed} == {starting_id, admitted_id}
    await service.complete_admission(
        tenant,
        admitted_id,
        claim_token="claim-cluster-refresh",
        workflow_run_id=RunId(new_id("run")),
        admitted_by="system:sourcing",
        admitted_at=NOW + timedelta(seconds=1),
        actor=system,
    )
    facts = SourcingPriorityFactsInput(
        need_id=changed_need,
        cluster_id=cluster_id,
        cluster_member_count=9,
        facts_observed_at=NOW,
    )
    async with sessions() as session:
        before_count = await session.scalar(
            select(func.count()).select_from(SourcingPrioritySnapshotRow)
        )
        immutable_snapshot_ids = {
            row.admission_id: row.current_snapshot_id
            for row in (
                await session.execute(
                    select(SourcingAdmissionRow).where(
                        SourcingAdmissionRow.admission_id.in_(
                            [starting_id, admitted_id]
                        )
                    )
                )
            ).scalars()
        }
    assert before_count is not None

    refreshed = await service.refresh_cluster_admissions(
        tenant,
        changed_need,
        facts=facts,
        refreshed_at=NOW + timedelta(minutes=1),
        actor=system,
    )
    replayed = await service.refresh_cluster_admissions(
        tenant,
        changed_need,
        facts=facts,
        refreshed_at=NOW + timedelta(minutes=2),
        actor=system,
    )

    assert refreshed == replayed
    assert len(refreshed) == 3
    async with sessions() as session:
        repeated_count = await session.scalar(
            select(func.count()).select_from(SourcingPrioritySnapshotRow)
        )
    assert repeated_count == before_count + 3
    next_version = await service.refresh_cluster_admissions(
        tenant,
        changed_need,
        facts=SourcingPriorityFactsInput(
            need_id=changed_need,
            cluster_id=cluster_id,
            cluster_member_count=10,
            facts_observed_at=NOW + timedelta(minutes=3),
        ),
        refreshed_at=NOW + timedelta(minutes=3),
        actor=system,
    )
    assert len(next_version) == 3
    assert set(next_version).isdisjoint(refreshed)
    async with sessions() as session:
        after_count = await session.scalar(
            select(func.count()).select_from(SourcingPrioritySnapshotRow)
        )
        rows = {
            row.admission_id: row
            for row in (
                await session.execute(
                    select(SourcingAdmissionRow).where(
                        SourcingAdmissionRow.admission_id.in_(
                            [
                                first_id,
                                second_id,
                                changed_id,
                                starting_id,
                                admitted_id,
                                other_cluster_id,
                                cross_tenant_id,
                            ]
                        )
                    )
                )
            ).scalars()
        }
        snapshots = {
            row.snapshot_id: row
            for row in (
                await session.execute(select(SourcingPrioritySnapshotRow))
            ).scalars()
        }
    assert after_count == before_count + 6
    for admission_id in (first_id, second_id, changed_id):
        row = rows[str(admission_id)]
        snapshot = snapshots[row.current_snapshot_id]
        assert (row.tenant_id, snapshot.cluster_id, snapshot.cluster_member_count) == (
            str(tenant),
            str(cluster_id),
            10,
        )
        assert row.state == AdmissionState.WAITING.value
    assert rows[str(other_cluster_id)].current_snapshot_id not in set(refreshed)
    assert rows[str(cross_tenant_id)].current_snapshot_id not in set(refreshed)
    assert rows[str(starting_id)].state == AdmissionState.STARTING.value
    assert rows[str(admitted_id)].state == AdmissionState.ADMITTED.value
    assert (
        rows[str(starting_id)].current_snapshot_id
        == immutable_snapshot_ids[str(starting_id)]
    )
    assert (
        rows[str(admitted_id)].current_snapshot_id
        == immutable_snapshot_ids[str(admitted_id)]
    )


@pytest.mark.asyncio
async def test_cluster_block_is_tenant_bound_state_safe_and_idempotent_in_postgres(
    integration_engine: AsyncEngine,
) -> None:
    """永久非法事实整簇阻断 waiting；既有阻断、终态与其他租户不可改写。"""

    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    cluster_id = NeedClusterId(new_id("ncl"))
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant, SourcingScope.SYSTEM, "system")
    other_system = SourcingActor(
        "system-worker", other_tenant, SourcingScope.SYSTEM, "system"
    )
    service = _service_type()(
        lambda bound: SqlAlchemySourcingUnitOfWork(sessions, bound),
        Phase2SourcingAuthorizer(tenant),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    other_service = _service_type()(
        lambda bound: SqlAlchemySourcingUnitOfWork(sessions, bound),
        Phase2SourcingAuthorizer(other_tenant),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )

    async def enqueue(
        bound_service: Any,
        bound_tenant: TenantId,
        actor: SourcingActor,
        suffix: str,
        *,
        target_cluster: NeedClusterId | None,
        blocked: bool = False,
        member_count: int = 2,
    ) -> tuple[ValidatedNeedId, SourcingAdmissionId]:
        need_id = ValidatedNeedId(new_id(f"need{suffix}"))
        await _seed_need(integration_engine, bound_tenant, need_id)
        case_id = await bound_service.open_case(
            bound_tenant, _command(bound_tenant, need_id), actor=actor
        )
        command = (
            SourcingAdmissionEnqueueCommand(
                facts=None,
                blocked_reason="priority_facts_invalid",
            )
            if blocked
            else SourcingAdmissionEnqueueCommand(
                facts=SourcingPriorityFactsInput(
                    need_id=need_id,
                    cluster_id=target_cluster,
                    cluster_member_count=member_count,
                    facts_observed_at=NOW - timedelta(minutes=1),
                )
            )
        )
        admission_id = await bound_service.enqueue_admission(
            bound_tenant,
            case_id,
            need_id,
            ready_at=NOW - timedelta(hours=1),
            command=command,
            actor=actor,
        )
        return need_id, admission_id

    _, first_id = await enqueue(
        service, tenant, system, "first", target_cluster=cluster_id
    )
    _, second_id = await enqueue(
        service, tenant, system, "second", target_cluster=cluster_id
    )
    changed_need, changed_id = await enqueue(
        service,
        tenant,
        system,
        "changed",
        target_cluster=None,
        blocked=True,
    )
    _, other_cluster_id = await enqueue(
        service,
        tenant,
        system,
        "other",
        target_cluster=NeedClusterId(new_id("ncl")),
    )
    _, starting_id = await enqueue(
        service,
        tenant,
        system,
        "starting",
        target_cluster=cluster_id,
        member_count=20,
    )
    _, admitted_id = await enqueue(
        service,
        tenant,
        system,
        "admitted",
        target_cluster=cluster_id,
        member_count=20,
    )
    _, case_mismatch_id = await enqueue(
        service, tenant, system, "mismatch", target_cluster=cluster_id
    )
    _, cross_tenant_id = await enqueue(
        other_service,
        other_tenant,
        other_system,
        "cross",
        target_cluster=cluster_id,
    )
    claimed = await service.claim_admissions(
        tenant,
        limit=2,
        claim_token="claim-cluster-block",
        claim_expires_at=NOW + timedelta(minutes=10),
        actor=system,
    )
    assert {item.admission_id for item in claimed} == {starting_id, admitted_id}
    await service.complete_admission(
        tenant,
        admitted_id,
        claim_token="claim-cluster-block",
        workflow_run_id=RunId(new_id("run")),
        admitted_by="system:sourcing",
        admitted_at=NOW + timedelta(seconds=1),
        actor=system,
    )
    await service.block_admission(
        tenant,
        case_mismatch_id,
        reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
        blocked_at=NOW,
        actor=system,
    )
    immutable_ids = (
        other_cluster_id,
        starting_id,
        admitted_id,
        case_mismatch_id,
        cross_tenant_id,
    )
    async with sessions() as session:
        before_snapshots = await session.scalar(
            select(func.count())
            .select_from(SourcingPrioritySnapshotRow)
            .where(
                SourcingPrioritySnapshotRow.tenant_id.in_(
                    [str(tenant), str(other_tenant)]
                )
            )
        )
        immutable_before = {
            row.admission_id: (
                row.state,
                row.blocked_reason,
                row.current_snapshot_id,
                row.updated_at,
            )
            for row in (
                await session.execute(
                    select(SourcingAdmissionRow).where(
                        SourcingAdmissionRow.tenant_id.in_(
                            [str(tenant), str(other_tenant)]
                        ),
                        SourcingAdmissionRow.admission_id.in_(immutable_ids)
                    )
                )
            ).scalars()
        }

    blocked = await service.block_cluster_admissions(
        tenant,
        cluster_id,
        changed_need,
        blocked_at=NOW - timedelta(minutes=1),
        actor=system,
    )
    replayed = await service.block_cluster_admissions(
        tenant,
        cluster_id,
        changed_need,
        blocked_at=NOW + timedelta(minutes=2),
        actor=system,
    )

    assert blocked == replayed == tuple(sorted((first_id, second_id, changed_id)))
    async with sessions() as session:
        after_snapshots = await session.scalar(
            select(func.count())
            .select_from(SourcingPrioritySnapshotRow)
            .where(
                SourcingPrioritySnapshotRow.tenant_id.in_(
                    [str(tenant), str(other_tenant)]
                )
            )
        )
        rows = {
            row.admission_id: row
            for row in (
                await session.execute(
                    select(SourcingAdmissionRow).where(
                        SourcingAdmissionRow.tenant_id.in_(
                            [str(tenant), str(other_tenant)]
                        ),
                        SourcingAdmissionRow.admission_id.in_(
                            [first_id, second_id, changed_id, *immutable_ids]
                        )
                    )
                )
            ).scalars()
        }
    assert after_snapshots == before_snapshots
    for admission_id in (first_id, second_id, changed_id):
        row = rows[str(admission_id)]
        assert row.state == AdmissionState.BLOCKED.value
        assert row.blocked_reason == AdmissionBlockedReason.PRIORITY_FACTS_INVALID.value
        assert row.updated_at == NOW
    for admission_id in immutable_ids:
        row = rows[str(admission_id)]
        assert (
            row.state,
            row.blocked_reason,
            row.current_snapshot_id,
            row.updated_at,
        ) == immutable_before[str(admission_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing_snapshot", "缺少已验证需求快照"),
        ("snapshot_need_mismatch", "Case、Need 与快照不一致"),
        ("completeness_two", "完整度不足 3"),
        ("non_opened", "OPENED"),
    ],
)
async def test_admission_enqueue_rejects_ineligible_case_without_postgres_writes(
    integration_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    message: str,
) -> None:
    """真实事务中不合格 Case 必须在 admission repository 首次写入前关闭。

    数据库以 NOT NULL 与 JSON-object CHECK 双重拒绝无快照行；该分支在真实读取后注入
    丢失快照的 repository 投影，其余分支直接修改真实 PostgreSQL Case 行。
    """

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    await _seed_need(integration_engine, tenant_id, need_id)
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sessions, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    case_id = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )

    parameters: dict[str, object] = {"tenant": tenant_id, "case": case_id}
    mutation = None
    if fault == "missing_snapshot":
        repository_module = importlib.import_module("infra.db.repositories.sourcing")
        original_row_to_case = repository_module._row_to_case
        monkeypatch.setattr(
            repository_module,
            "_row_to_case",
            lambda row: replace(
                original_row_to_case(row),
                need_snapshot=None,
                need_snapshot_hash=None,
            ),
        )
    elif fault == "snapshot_need_mismatch":
        mutation = text(
            "UPDATE sourcing_cases SET need_snapshot = jsonb_set("
            "need_snapshot, '{need_id}', to_jsonb(CAST(:other_need AS text))) "
            "WHERE tenant_id = :tenant AND case_id = :case"
        )
        parameters["other_need"] = ValidatedNeedId(new_id("need"))
    elif fault == "completeness_two":
        mutation = text(
            "UPDATE sourcing_cases SET need_snapshot = jsonb_set("
            "need_snapshot, '{completeness}', CAST('2' AS jsonb)) "
            "WHERE tenant_id = :tenant AND case_id = :case"
        )
    else:
        mutation = text(
            "UPDATE sourcing_cases SET state = 'discovering' "
            "WHERE tenant_id = :tenant AND case_id = :case"
        )
    if mutation is not None:
        async with integration_engine.begin() as connection:
            await connection.execute(mutation, parameters)

    with pytest.raises(ValidationError, match=message):
        await service.enqueue_admission(
            tenant_id,
            case_id,
            need_id,
            ready_at=NOW - timedelta(minutes=1),
            command=SourcingAdmissionEnqueueCommand(
                facts=SourcingPriorityFactsInput(
                    need_id=need_id,
                    cluster_id=NeedClusterId(new_id("cluster")),
                    cluster_member_count=4,
                    facts_observed_at=NOW - timedelta(minutes=1),
                )
            ),
            actor=system,
        )

    async with sessions() as session:
        admission_count = await session.scalar(
            select(func.count())
            .select_from(SourcingAdmissionRow)
            .where(SourcingAdmissionRow.tenant_id == str(tenant_id))
        )
        snapshot_count = await session.scalar(
            select(func.count())
            .select_from(SourcingPrioritySnapshotRow)
            .where(SourcingPrioritySnapshotRow.tenant_id == str(tenant_id))
        )
    assert admission_count == 0
    assert snapshot_count == 0


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
                required="hinges" if name == "product_type" else f"required-{name}",
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


class _PriorityReader:
    def __init__(self, need_id: ValidatedNeedId) -> None:
        self._need_id = need_id

    async def get_cluster_priority_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedClusterPriorityFacts:
        assert need_id == self._need_id
        return NeedClusterPriorityFacts(
            need_id=str(need_id),
            cluster_id=None,
            cluster_member_count=1,
            facts_observed_at=NOW,
        )


class _UnknownFailureFirstEnqueue:
    def __init__(self, sourcing: Any) -> None:
        self._sourcing = sourcing
        self.calls = 0

    async def open_case(self, *args: Any, **kwargs: Any) -> Any:
        return await self._sourcing.open_case(*args, **kwargs)

    async def enqueue_admission(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("untrusted admission storage failure")
        return await self._sourcing.enqueue_admission(*args, **kwargs)


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
        cases = cast(Any, self.__dict__["cases"])
        self.cases = cast(Any, _BarrierCases(cases, self._barrier))
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
        candidates = cast(Any, self.__dict__["candidates"])
        self.candidates = cast(
            Any,
            _SealReadCandidates(candidates, self._read_complete, self._resume),
        )
        return entered


async def _public_verification_scenario(
    integration_engine: AsyncEngine,
    *,
    sealed: bool = True,
    descending_tiers: bool = False,
) -> dict[str, Any]:
    """用真实服务/UoW 建立一个尚未核验的公开草稿场景。"""

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    artifact_id = ArtifactId(new_id("art"))
    await _seed_need(integration_engine, tenant_id, need_id)
    await _seed_candidate_artifact(integration_engine, tenant_id, artifact_id)
    sf = async_sessionmaker(integration_engine, expire_on_commit=False)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    boss = SourcingActor("emp-boss", tenant_id, SourcingScope.TENANT, "boss")
    projection = CandidateEvidenceSnapshot(
        tenant_id=tenant_id,
        artifact_id=artifact_id,
        canonical_url="https://factory.example/public-hinge",
        content_hash="c" * 64,
        observed_at=NOW,
    )
    service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _FixedEvidenceReader(projection),
        now=lambda: NOW,
    )
    open_command = _command(tenant_id, need_id)
    provenance = open_command.need.product_category.provenance
    need = open_command.need.model_copy(
        update={
            "material": NeedFact(value="steel", provenance=provenance),
            "size_spec": NeedFact(value="4 inch", provenance=provenance),
            "model": NeedFact(value="HX-4", provenance=provenance),
        }
    )
    case_id = await service.open_case(
        tenant_id,
        open_command.model_copy(update={"need": need}),
        actor=system,
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
                PublicSourcingQuery(query_text="hinge factory", target_country="US"),
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
    await service.authorize_public_plan_run(
        tenant_id, case_id, plan.plan_id, plan.plan_hash, actor=boss
    )
    run_id = RunId(new_id("run"))
    async with integration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id, tenant_id, workflow_type, workflow_version, subject_ref, "
                "current_step, status, context, idempotency_key) VALUES "
                "(:run, :tenant, 'sourcing_case', 2, :case, 'verify_candidates', "
                "'running', '{}', :key)"
            ),
            {
                "run": run_id,
                "tenant": tenant_id,
                "case": case_id,
                "key": f"verify:{run_id}",
            },
        )
    draft = PublicCandidateDraft(
        draft_id=new_id("scd"),
        tenant_id=tenant_id,
        case_id=case_id,
        run_id=run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        query_index=0,
        result_index=0,
        source_key="f" * 64,
        supplier_name="Factory A",
        product_title="Stainless hinge HX-4",
        specs=tuple(
            PublicCandidateDraftSpec(
                spec_name=name,
                required=required,
                observed=required,
            )
            for name, required in (
                ("product_type", "hinges"),
                ("material", "steel"),
                ("size", "4 inch"),
                ("model", "HX-4"),
            )
        ),
        moq=500 if sealed else 6000,
        indicative_price_tiers=(
            *(
                (
                    PublicCandidateDraftPriceTier(
                        minimum_quantity=2000,
                        amount=Decimal("1.00"),
                        currency="USD",
                        unit="piece",
                    ),
                )
                if descending_tiers
                else ()
            ),
            PublicCandidateDraftPriceTier(
                minimum_quantity=1000,
                amount=Decimal("1.25"),
                currency="USD",
                unit="piece",
            ),
        ),
        rejection_codes=(),
        evidence_url=projection.canonical_url,
        evidence_observed_at=projection.observed_at,
        evidence_hash=projection.content_hash,
        evidence_artifact_ref=artifact_id,
        created_at=NOW,
    )
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        await uow.candidate_drafts.get_or_create_canonical(tenant_id, draft)
    command = VerifyPublicCandidateDraftsCommand(
        run_id=run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        draft_ids=(draft.draft_id,),
    )
    async with sf() as session:
        case_version = await session.scalar(
            select(SourcingCaseRow.version).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
    assert case_version is not None
    return {
        "tenant_id": tenant_id,
        "case_id": case_id,
        "source_key": draft.source_key,
        "command": command,
        "projection": projection,
        "session_factory": sf,
        "system": system,
        "case_version": case_version,
        "service": service,
        "draft": draft,
    }


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
async def test_outbox_retries_sanitized_enqueue_failure_and_recovers_one_admission(
    integration_engine: AsyncEngine,
) -> None:
    """开案已提交而准入入队临时失败时，Outbox 必须可恢复且不泄密。"""

    from apps.scheduler_worker.sourcing_events import SourcingTriggerHandler
    from infra.db.outbox import PostgresEventBus
    from infra.db.outbox_delivery import OutboxDeliverer

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    await _seed_need(integration_engine, tenant_id, need_id)
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    clock = _Clock(NOW)
    system = SourcingActor("system-worker", tenant_id, SourcingScope.SYSTEM, "system")
    sourcing = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(factory, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=clock.now,
    )
    flaky_sourcing = _UnknownFailureFirstEnqueue(sourcing)
    command = _command(tenant_id, need_id)
    handler = SourcingTriggerHandler(
        sourcing=cast(Any, flaky_sourcing),
        demand=_PriorityReader(need_id),
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
        admission_count = await connection.scalar(
            text(
                "SELECT count(*) FROM sourcing_admissions "
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
    assert admission_count == 0
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
        admission_count = await connection.scalar(
            text(
                "SELECT count(*) FROM sourcing_admissions "
                "WHERE tenant_id=:tenant AND need_id=:need"
            ),
            {"tenant": str(tenant_id), "need": str(need_id)},
        )
        priority_snapshot_count = await connection.scalar(
            text(
                "SELECT count(*) FROM sourcing_priority_snapshots p "
                "JOIN sourcing_admissions a "
                "ON a.tenant_id=p.tenant_id AND a.admission_id=p.admission_id "
                "WHERE a.tenant_id=:tenant AND a.need_id=:need"
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
    assert admission_count == 1
    assert priority_snapshot_count == 1
    assert runs == []
    assert flaky_sourcing.calls == 2


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
@pytest.mark.parametrize("sealed", [False, True])
async def test_concurrent_public_draft_verification_replays_canonical_tiers_in_postgres(
    integration_engine: AsyncEngine,
    sealed: bool,
) -> None:
    """降序 tier 的合格/拒绝草稿并发重放都只生成一个 canonical Candidate。"""

    scenario = await _public_verification_scenario(
        integration_engine,
        sealed=sealed,
        descending_tiers=True,
    )
    tenant_id = scenario["tenant_id"]
    case_id = scenario["case_id"]
    sf = scenario["session_factory"]
    system = scenario["system"]
    service = scenario["service"]
    draft = scenario["draft"]
    command = scenario["command"]

    first, second = await asyncio.gather(
        service.verify_public_candidate_drafts(
            tenant_id, case_id, command, actor=system
        ),
        service.verify_public_candidate_drafts(
            tenant_id, case_id, command, actor=system
        ),
    )

    assert first == second
    assert (first.verified_event is not None) is sealed
    async with SqlAlchemySourcingUnitOfWork(sf, tenant_id) as uow:
        stored_candidate = await uow.candidates.get_by_public_draft_source_key(
            tenant_id, draft.source_key
        )
    assert stored_candidate is not None
    assert [
        tier.minimum_quantity for tier in stored_candidate.indicative_price_tiers
    ] == [1000, 2000]
    async with sf() as session:
        candidate_count = await session.scalar(
            text(
                "SELECT count(*) FROM sourcing_candidates "
                "WHERE tenant_id=:tenant AND public_draft_source_key=:source"
            ),
            {"tenant": str(tenant_id), "source": draft.source_key},
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCandidatesVerified",
            )
        )
        case_row = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
    assert candidate_count == 1
    assert event_count == int(sealed)
    assert case_row is not None
    if first.verified_event is not None:
        assert case_row.version == first.verified_event.case_version
        assert tuple(case_row.sealed_candidate_ids) == tuple(
            map(str, first.qualified_candidate_ids)
        )
    else:
        assert tuple(case_row.sealed_candidate_ids) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["candidate", "case", "outbox"])
async def test_public_draft_verification_rolls_back_each_real_postgres_write_stage(
    integration_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """Candidate、Case seal、Outbox 任一写后失败都不得留下部分 generation。"""

    from infra.db.outbox import PostgresEventBus
    from infra.db.repositories.sourcing import (
        CandidateRepositoryImpl,
        SourcingCaseRepositoryImpl,
    )

    scenario = await _public_verification_scenario(integration_engine)
    tenant_id = scenario["tenant_id"]
    case_id = scenario["case_id"]
    command = scenario["command"]
    projection = scenario["projection"]
    sf = scenario["session_factory"]
    system = scenario["system"]
    if failure_stage == "candidate":
        original = CandidateRepositoryImpl.get_or_create_public_draft

        async def fail_after_candidate(self: Any, *args: Any, **kwargs: Any) -> Any:
            await original(self, *args, **kwargs)
            raise RuntimeError("candidate write fault")

        target: type[Any] = CandidateRepositoryImpl
        method_name = "get_or_create_public_draft"
        injected = fail_after_candidate
    elif failure_stage == "case":
        original = SourcingCaseRepositoryImpl.update

        async def fail_after_case(self: Any, *args: Any, **kwargs: Any) -> Any:
            await original(self, *args, **kwargs)
            raise RuntimeError("case write fault")

        target = SourcingCaseRepositoryImpl
        method_name = "update"
        injected = fail_after_case
    else:
        original = PostgresEventBus.publish

        async def fail_after_outbox(self: Any, *args: Any, **kwargs: Any) -> Any:
            await original(self, *args, **kwargs)
            await self._session.flush()
            raise RuntimeError("outbox write fault")

        target = PostgresEventBus
        method_name = "publish"
        injected = fail_after_outbox
    monkeypatch.setattr(target, method_name, injected)
    failing_service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _FixedEvidenceReader(projection),
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match=f"^{failure_stage} write fault$"):
        await failing_service.verify_public_candidate_drafts(
            tenant_id, case_id, command, actor=system
        )

    monkeypatch.setattr(target, method_name, original)
    async with sf() as session:
        candidate_count = await session.scalar(
            text(
                "SELECT count(*) FROM sourcing_candidates "
                "WHERE tenant_id=:tenant AND public_draft_source_key=:source"
            ),
            {"tenant": str(tenant_id), "source": scenario["source_key"]},
        )
        case_row = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCandidatesVerified",
            )
        )
    assert candidate_count == 0
    assert case_row is not None
    assert case_row.version == scenario["case_version"]
    assert tuple(case_row.sealed_candidate_ids) == ()
    assert case_row.candidate_set_hash is None
    assert case_row.candidates_verified_at is None
    assert event_count == 0

    retry_service = _service_type()(
        lambda bound_tenant: SqlAlchemySourcingUnitOfWork(sf, bound_tenant),
        Phase2SourcingAuthorizer(tenant_id),
        _FixedEvidenceReader(projection),
        now=lambda: NOW,
    )
    result = await retry_service.verify_public_candidate_drafts(
        tenant_id, case_id, command, actor=system
    )

    assert result.verified_event is not None
    async with sf() as session:
        retry_candidate_count = await session.scalar(
            text(
                "SELECT count(*) FROM sourcing_candidates "
                "WHERE tenant_id=:tenant AND public_draft_source_key=:source"
            ),
            {"tenant": str(tenant_id), "source": scenario["source_key"]},
        )
        retry_case = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
        retry_event_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCandidatesVerified",
            )
        )
    assert retry_candidate_count == 1
    assert retry_case is not None
    assert tuple(retry_case.sealed_candidate_ids) == tuple(
        map(str, result.qualified_candidate_ids)
    )
    assert retry_event_count == 1


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
                PublicSourcingQuery(query_text="hinge factory", target_country="US"),
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
                PublicSourcingQuery(query_text="hinge factory", target_country="US"),
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
        tenant_id,
        case_id,
        opportunity_id,
        expected_need_id=need_id,
        actor=system,
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
