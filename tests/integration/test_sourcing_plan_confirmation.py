"""Task 8 公开寻源计划门禁、Workflow 投递与保守恢复的真实 PostgreSQL 合同。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from connectors.search_contracts import SearchCostStatus, SearchUsage
from domains.sourcing.errors import SourcingPlanStaleError
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.schemas import (
    NeedFact,
    OpenSourcingCase,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingNeedSnapshot,
    SourcingUncertainReconciliationCommand,
)
from domains.sourcing.service import (
    LadderCheck,
    LadderOutcome,
    MatchLadderRung,
    ProviderUsageEvidenceSnapshot,
    PublicPlanStatus,
    SourcingSearchExecution,
    SourcingSearchExecutionStatus,
)
from domains.sourcing.service_impl import SourcingServiceImpl
from infra.db.search_quota import PostgresSearchQuotaRepository
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import (
    SearchQuotaAccountRow,
    SourcingPublicPlanRow,
    SourcingSearchReconciliationRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType
from workflows.sourcing_case.application import (
    SourcingCaseApplication,
    SourcingPlanDeliveryError,
)
from workflows.sourcing_case.flow import build_sourcing_case_definition
from workflows.sourcing_case.steps import AwaitPublicPlanStep, FixedWaitStep

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


async def _clear_quota(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM search_quota_reservations"))
        await connection.execute(text("DELETE FROM search_quota_runs"))
        await connection.execute(text("DELETE FROM search_quota_accounts"))


@pytest_asyncio.fixture(autouse=True)
async def _isolate_single_account(
    integration_engine: AsyncEngine,
) -> AsyncIterator[None]:
    """单部署 Tavily 账户是全局唯一；本文件不得污染相邻额度测试。"""

    await _clear_quota(integration_engine)
    try:
        yield
    finally:
        await _clear_quota(integration_engine)


class _UnusedCandidateEvidenceReader:
    async def read_verified(self, tenant_id: TenantId, artifact_id: ArtifactId):
        raise AssertionError("计划与恢复路径不得读取候选网页证据")


class _UsageReader:
    def __init__(self, tenant_id: TenantId, artifact_id: ArtifactId) -> None:
        self._snapshot = ProviderUsageEvidenceSnapshot(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            provider="tavily",
            content_hash="e" * 64,
            observed_at=NOW,
        )

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> ProviderUsageEvidenceSnapshot:
        assert (tenant_id, artifact_id) == (
            self._snapshot.tenant_id,
            self._snapshot.artifact_id,
        )
        return self._snapshot


class _FailFirstAcknowledgement:
    """模拟审计事实提交后、额度收紧前崩溃。"""

    def __init__(self, delegate: PostgresSearchQuotaRepository) -> None:
        self._delegate = delegate

    async def snapshot(self):
        return await self._delegate.snapshot()

    async def get(self, run_id: RunId, request_key: str):
        return await self._delegate.get(run_id, request_key)

    async def acknowledge_uncertain_as_consumed(
        self, run_id: RunId, request_key: str
    ) -> None:
        raise TransientError("provider-token-must-not-escape")


def _open_command(
    tenant_id: TenantId, need_id: ValidatedNeedId
) -> OpenSourcingCase:
    provenance = ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="message-plan-integration",
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


def _plan(
    case_id: SourcingCaseId,
    version: int,
    expected_case_version: int,
) -> PublicSourcingPlanCommand:
    return PublicSourcingPlanCommand(
        plan_id=SourcingPlanId(new_id("spl")),
        case_id=case_id,
        target_countries=("US",),
        product_category="hinges",
        queries=(
            PublicSourcingQuery(
                query_text=f"hinge factory scope {version}", target_country="US"
            ),
        ),
        max_search_queries=1,
        max_pages_read=2,
        provider="tavily",
        search_depth="basic",
        usage_credits_remaining=10,
        worst_case_credits=2,
        version=version,
        expected_case_version=expected_case_version,
    )


async def _seed_need(
    engine: AsyncEngine, tenant_id: TenantId, need_id: ValidatedNeedId
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id, need_id, account_id, product_category, source_message_id, "
                "status, created_at) VALUES (:tenant, :need, 'account-plan', "
                "CAST(:category AS jsonb), 'message-plan', 'sourcing_ready', :created_at)"
            ),
            {
                "tenant": tenant_id,
                "need": need_id,
                "category": '{"value":"hinges"}',
                "created_at": NOW,
            },
        )


async def _seed_artifact(
    engine: AsyncEngine, tenant_id: TenantId, artifact_id: ArtifactId
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO raw_artifacts "
                "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, "
                "object_key, uploaded_at) VALUES (:tenant, :artifact, 'web_snapshot', "
                ":hash, 1, 'text/html', :key, :created_at)"
            ),
            {
                "tenant": tenant_id,
                "artifact": artifact_id,
                "hash": "e" * 64,
                "key": f"raw/{tenant_id}/{artifact_id}",
                "created_at": NOW,
            },
        )


async def _prepare_case(
    service: SourcingServiceImpl,
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
    system: SourcingActor,
) -> SourcingCaseId:
    case_id = await service.open_case(
        tenant_id, _open_command(tenant_id, need_id), actor=system
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
                input_snapshot={"rung": rung},
                input_snapshot_hash="a" * 64,
                conclusion=f"no qualified supply at rung {rung}",
                match_object_type=None,
                match_object_id=None,
                spec_comparisons=(),
                evidence_refs=(),
                checked_by=EmployeeId("untrusted"),
                checked_at=NOW,
            ),
            actor=system,
        )
    return case_id


async def _seed_waiting_run(
    factory,
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    need_id: ValidatedNeedId,
) -> tuple[PostgresWorkflowEngine, RunId]:
    definition = build_sourcing_case_definition()
    handlers = {
        step.handler_ref: FixedWaitStep("integration_wait")
        for step in definition.steps
    }
    handlers["sourcing_case.v2.await_public_plan"] = AwaitPublicPlanStep()
    engine = PostgresWorkflowEngine(factory, handlers, now=lambda: NOW)
    engine.register(definition)
    run_id = RunId(new_id("run"))
    async with factory() as session, session.begin():
        session.add(
            WorkflowRunRow(
                run_id=run_id,
                tenant_id=tenant_id,
                workflow_type="sourcing_case",
                workflow_version=2,
                subject_ref=case_id,
                current_step="await_public_plan",
                status="running",
                created_at=NOW,
                context={
                    "case_id": str(case_id),
                    "need_id": str(need_id),
                    "need_snapshot_hash": "a" * 64,
                },
                idempotency_key=f"sourcing-plan:{case_id}",
            )
        )
        session.add(
            WorkflowStepRow(
                step_id=new_id("wfs"),
                run_id=run_id,
                tenant_id=tenant_id,
                step_name="await_public_plan",
                status="waiting_event",
                data={"planned_at": NOW.isoformat()},
                attempt=0,
                due_at=NOW,
                idempotency_key=f"{tenant_id}:wfstep:{run_id}:await_public_plan",
            )
        )
    return engine, run_id


async def _seed_free_account(factory, tenant_id: TenantId) -> None:
    async with factory() as session, session.begin():
        session.add(
            SearchQuotaAccountRow(
                tenant_id=tenant_id,
                provider="tavily",
                ceiling=10,
                reservations=0,
                cost_status=SearchCostStatus.FREE.value,
                usage_limit=10,
                usage_used=0,
                paygo_enabled=False,
                checked_at=NOW,
            )
        )


@pytest.mark.asyncio
async def test_plan_replacement_concurrent_run_and_reconciliation_recover_after_restart(
    integration_engine: AsyncEngine,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    evidence_id = ArtifactId(new_id("art"))
    await _seed_need(integration_engine, tenant_id, need_id)
    await _seed_artifact(integration_engine, tenant_id, evidence_id)
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    await _seed_free_account(factory, tenant_id)
    system = SourcingActor("system-plan", tenant_id, SourcingScope.SYSTEM, "system")
    boss = SourcingActor("boss-plan", tenant_id, SourcingScope.TENANT, "boss")
    sourcing_actor = SourcingActor(
        "sourcing-plan", tenant_id, SourcingScope.TENANT, "sourcing"
    )
    usage_reader = _UsageReader(tenant_id, evidence_id)

    def make_service() -> SourcingServiceImpl:
        return SourcingServiceImpl(
            lambda bound: SqlAlchemySourcingUnitOfWork(factory, bound),
            Phase2SourcingAuthorizer(tenant_id),
            _UnusedCandidateEvidenceReader(),
            provider_usage_evidence_reader=usage_reader,
            now=lambda: NOW,
        )

    service = make_service()
    case_id = await _prepare_case(service, tenant_id, need_id, system)
    engine, run_id = await _seed_waiting_run(factory, tenant_id, case_id, need_id)
    quota = PostgresSearchQuotaRepository(factory, tenant_id, now=lambda: NOW)
    application = SourcingCaseApplication(
        sourcing=service, quota=quota, engine=engine
    )

    first = await application.create_plan(
        tenant_id, case_id, _plan(case_id, 1, 6), actor=sourcing_actor
    )
    await application.confirm_plan(
        tenant_id, case_id, first.plan_id, first.plan_hash, actor=boss
    )
    replacement = await application.create_plan(
        tenant_id, case_id, _plan(case_id, 2, 7), actor=sourcing_actor
    )
    with pytest.raises(SourcingPlanStaleError):
        await application.run(
            tenant_id, case_id, first.plan_id, first.plan_hash, actor=boss
        )
    await application.confirm_plan(
        tenant_id,
        case_id,
        replacement.plan_id,
        replacement.plan_hash,
        actor=boss,
    )

    running = await asyncio.gather(
        *(
            application.run(
                tenant_id,
                case_id,
                replacement.plan_id,
                replacement.plan_hash,
                actor=boss,
            )
            for _ in range(2)
        )
    )
    assert {item.status for item in running} == {PublicPlanStatus.RUNNING}
    progressed = await engine.get_run(tenant_id, run_id)
    assert progressed is not None and progressed.current_step == "public_search"
    assert progressed.context["sourcing_plan_id"] == str(replacement.plan_id)
    assert progressed.context["sourcing_plan_hash"] == replacement.plan_hash
    assert await engine.poll_due(tenant_id, 1) == 1

    request_key = "d" * 64
    await quota.reserve(
        run_id,
        request_key,
        SearchUsage("Researcher", 10, 0, False, SearchCostStatus.FREE),
        fingerprint_version="v1",
    )
    await quota.mark_dispatched(run_id, request_key)
    execution = SourcingSearchExecution(
        execution_id=new_id("sse"),
        tenant_id=tenant_id,
        case_id=case_id,
        plan_id=replacement.plan_id,
        run_id=str(run_id),
        plan_hash=replacement.plan_hash,
        query_index=0,
        request_key=request_key,
        query_hash="c" * 64,
        locator_results=(),
        provider_status=SourcingSearchExecutionStatus.UNCERTAIN,
        created_at=NOW,
    )
    async with SqlAlchemySourcingUnitOfWork(factory, tenant_id) as uow:
        await uow.search_executions.add(tenant_id, execution)
    command = SourcingUncertainReconciliationCommand(
        reconciliation_id=new_id("srr"),
        run_id=run_id,
        request_key=request_key,
        resolution="count_as_consumed",
        reason="已按提供商账户用量原件核对为已消耗",
        provider_usage_artifact_ref=evidence_id,
    )

    crashing = SourcingCaseApplication(
        sourcing=service,
        quota=_FailFirstAcknowledgement(quota),  # type: ignore[arg-type]
        engine=engine,
    )
    with pytest.raises(SourcingPlanDeliveryError) as failure:
        await crashing.reconcile_uncertain(
            tenant_id, case_id, command, actor=boss
        )
    assert "token" not in str(failure.value)
    assert (await quota.get(run_id, request_key)).status == "uncertain"  # type: ignore[union-attr]

    restarted_engine, _ = await _rebuild_engine(factory, run_id)
    restarted_quota = PostgresSearchQuotaRepository(
        factory, tenant_id, now=lambda: NOW + timedelta(minutes=1)
    )
    restarted = SourcingCaseApplication(
        sourcing=make_service(), quota=restarted_quota, engine=restarted_engine
    )
    first_recovery = await restarted.reconcile_uncertain(
        tenant_id, case_id, command, actor=boss
    )
    second_recovery = await restarted.reconcile_uncertain(
        tenant_id, case_id, command, actor=boss
    )
    assert first_recovery == second_recovery
    assert (await restarted_quota.get(run_id, request_key)).status == "consumed"  # type: ignore[union-attr]
    snapshot = await restarted_quota.snapshot()
    assert snapshot is not None and snapshot.reservations == 1
    other_tenant = TenantId(new_id("tn"))
    async with SqlAlchemySourcingUnitOfWork(factory, other_tenant) as other_uow:
        assert (
            await other_uow.reconciliations.get_for_execution(
                other_tenant, execution.execution_id
            )
            is None
        )
    async with factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(SourcingSearchReconciliationRow)
            .where(SourcingSearchReconciliationRow.tenant_id == tenant_id)
        ) == 1
        assert await session.scalar(
            select(SourcingPublicPlanRow.status).where(
                SourcingPublicPlanRow.tenant_id == tenant_id,
                SourcingPublicPlanRow.plan_id == replacement.plan_id,
            )
        ) == "running"


@pytest.mark.asyncio
async def test_exact_event_replay_fails_closed_before_history_when_active_run_is_duplicate(
    integration_engine: AsyncEngine,
) -> None:
    """即使 exact event 已持久化，两个 active Case Run 也必须先触发唯一性拒绝。"""

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    evidence_id = ArtifactId(new_id("art"))
    await _seed_need(integration_engine, tenant_id, need_id)
    await _seed_artifact(integration_engine, tenant_id, evidence_id)
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    await _seed_free_account(factory, tenant_id)
    system = SourcingActor("system-duplicate", tenant_id, SourcingScope.SYSTEM, "system")
    boss = SourcingActor("boss-duplicate", tenant_id, SourcingScope.TENANT, "boss")
    service = SourcingServiceImpl(
        lambda bound: SqlAlchemySourcingUnitOfWork(factory, bound),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedCandidateEvidenceReader(),
        provider_usage_evidence_reader=_UsageReader(tenant_id, evidence_id),
        now=lambda: NOW,
    )
    case_id = await _prepare_case(service, tenant_id, need_id, system)
    engine, owning_run_id = await _seed_waiting_run(
        factory, tenant_id, case_id, need_id
    )
    application = SourcingCaseApplication(
        sourcing=service,
        quota=PostgresSearchQuotaRepository(factory, tenant_id, now=lambda: NOW),
        engine=engine,
    )
    plan = await application.create_plan(
        tenant_id, case_id, _plan(case_id, 1, 6), actor=boss
    )
    await application.confirm_plan(
        tenant_id, case_id, plan.plan_id, plan.plan_hash, actor=boss
    )
    await application.run(
        tenant_id, case_id, plan.plan_id, plan.plan_hash, actor=boss
    )
    payload = {"plan_id": str(plan.plan_id), "plan_hash": plan.plan_hash}
    assert await engine.has_delivered_event(
        tenant_id,
        "sourcing_case",
        str(case_id),
        "SourcingPlanConfirmed",
        payload,
        workflow_version=2,
        required_context={"case_id": str(case_id)},
    )

    duplicate_run_id = RunId(new_id("run"))
    async with factory() as session, session.begin():
        session.add(
            WorkflowRunRow(
                run_id=duplicate_run_id,
                tenant_id=tenant_id,
                workflow_type="sourcing_case",
                workflow_version=2,
                subject_ref=case_id,
                current_step="public_search",
                status="running",
                created_at=NOW,
                context={"case_id": str(case_id)},
                idempotency_key=f"duplicate-active:{case_id}",
            )
        )

    with pytest.raises(ValidationError, match="not unique"):
        await application.run(
            tenant_id, case_id, plan.plan_id, plan.plan_hash, actor=boss
        )
    async with factory() as session:
        active_ids = (
            await session.execute(
                select(WorkflowRunRow.run_id).where(
                    WorkflowRunRow.tenant_id == tenant_id,
                    WorkflowRunRow.workflow_type == "sourcing_case",
                    WorkflowRunRow.subject_ref == case_id,
                    WorkflowRunRow.status == "running",
                )
            )
        ).scalars().all()
    assert set(active_ids) == {str(owning_run_id), str(duplicate_run_id)}


async def _rebuild_engine(factory, run_id: RunId) -> tuple[PostgresWorkflowEngine, RunId]:
    definition = build_sourcing_case_definition()
    handlers = {
        step.handler_ref: FixedWaitStep("integration_wait")
        for step in definition.steps
    }
    handlers["sourcing_case.v2.await_public_plan"] = AwaitPublicPlanStep()
    engine = PostgresWorkflowEngine(factory, handlers, now=lambda: NOW)
    engine.register(definition)
    return engine, run_id
