"""候选产品投影的真实 PostgreSQL Product/Sourcing/Workflow 闭环。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from apps.scheduler_worker.sourcing_projections import (
    SourcingCandidateProductProjector,
)
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.service_impl import ProductServiceImpl
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.schemas import (
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingReviewCommand,
    VerifyPublicCandidateDraftsResult,
)
from domains.sourcing.service import (
    CandidateEvidenceSnapshot,
    LadderCheck,
    LadderOutcome,
    MatchLadderRung,
)
from domains.sourcing.service_impl import SourcingServiceImpl
from infra.db.products_uow import SqlAlchemyProductsUnitOfWork
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import (
    OutboxEventRow,
    ProductCandidateSourceRow,
    ProductRow,
    SourcingCaseRow,
    SourcingReviewRow,
    SourcingSupplyOptionRow,
)
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.errors import TransientError
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
from tests.integration.test_sourcing_service_persistence import (
    NOW,
    _candidate_submission,
    _command,
    _FixedEvidenceReader,
    _seed_candidate_artifact,
    _seed_need,
)
from workflows.engine.runner import (
    StepDefinition,
    StepStatus,
    WorkflowDefinition,
    WorkflowRun,
)
from workflows.sourcing_case.application import (
    SourcingCaseApplication,
    SourcingPlanDeliveryError,
)
from workflows.sourcing_case.steps import (
    AwaitProductCardsStep,
    PrepareCandidatesStep,
    VerifyCandidatesStep,
)


class _AwaitReview:
    async def execute(self, run: WorkflowRun):
        event = run.context.get("event")
        if not isinstance(event, dict) or event.get("event_type") != "SourcingReviewSubmitted":
            return ("wait", None, {})
        return (
            "advance",
            "handoff_costing",
            {"sourcing_stop_reason": "opportunity_required"},
        )


class _HandoffRetry:
    async def execute(self, run: WorkflowRun):
        event = run.context.get("event")
        if event is not None and (
            not isinstance(event, dict)
            or event.get("event_type") != "SourcingHandoffRetryRequested"
        ):
            raise AssertionError("交接步骤只接受精确重试事件")
        return ("wait", None, {"sourcing_stop_reason": "opportunity_required"})


class _UnusedQuota:
    pass


class _VerifiedBridge:
    """只给真实 Verify/Prepare handler 提供已封存域结果；不执行投影副作用。"""

    def __init__(self, result: VerifyPublicCandidateDraftsResult) -> None:
        self.result = result
        self.calls = 0

    async def verify_public_candidate_drafts(
        self, tenant_id: TenantId, case_id: SourcingCaseId, command: object, *, actor: object
    ) -> VerifyPublicCandidateDraftsResult:
        self.calls += 1
        assert tenant_id == self.result.verified_event.tenant_id
        assert case_id == self.result.verified_event.case_id
        return self.result


def _ladder_check(
    tenant_id: TenantId, case_id: SourcingCaseId, rung: int
) -> LadderCheck:
    return LadderCheck(
        check_id=new_id("slc"),
        tenant_id=tenant_id,
        case_id=case_id,
        sequence_number=rung,
        rung=MatchLadderRung(rung),
        outcome=LadderOutcome.NO_QUALIFIED_SUPPLY,
        input_snapshot={"category": "hinges"},
        input_snapshot_hash="b" * 64,
        conclusion="no_qualified_supply",
        match_object_type=None,
        match_object_id=None,
        spec_comparisons=(),
        evidence_refs=(),
        checked_by=EmployeeId("untrusted"),
        checked_at=NOW,
    )


@pytest.mark.asyncio
async def test_verified_projection_replay_persists_one_complete_generation(
    integration_engine: AsyncEngine,
) -> None:
    """重投必须复用真实 Product/Option，且 Ready Outbox 与内部唤醒各只一次。"""

    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    artifact_id = ArtifactId(new_id("art"))
    await _seed_need(integration_engine, tenant_id, need_id)
    await _seed_candidate_artifact(integration_engine, tenant_id, artifact_id)
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    sourcing_actor = SourcingActor(
        "system-projector", tenant_id, SourcingScope.SYSTEM, "system"
    )
    reviewer = SourcingActor(
        "employee-sourcing", tenant_id, SourcingScope.TENANT, "sourcing"
    )
    boss = SourcingActor("employee-boss", tenant_id, SourcingScope.TENANT, "boss")
    product_actor = ProductActor(
        "system-projector", ProductRole.SYSTEM, tenant_id
    )
    evidence = _FixedEvidenceReader(
        # 候选提交必须与已入库 Artifact 完全一致。
        CandidateEvidenceSnapshot(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            canonical_url="https://factory.example/hinge",
            content_hash="c" * 64,
            observed_at=NOW,
        )
    )
    sourcing = SourcingServiceImpl(
        lambda bound: SqlAlchemySourcingUnitOfWork(factory, bound),
        Phase2SourcingAuthorizer(tenant_id),
        evidence,
        now=lambda: NOW,
    )
    products = ProductServiceImpl(
        lambda bound: SqlAlchemyProductsUnitOfWork(factory, bound),
        Phase2ProductAuthorizer(tenant_id),
        now=lambda: NOW,
    )
    case_id = await sourcing.open_case(
        tenant_id, _command(tenant_id, need_id), actor=sourcing_actor
    )
    for rung in range(1, 6):
        await sourcing.record_ladder_check(
            tenant_id,
            case_id,
            _ladder_check(tenant_id, case_id, rung),
            actor=sourcing_actor,
        )
    plan = await sourcing.save_public_plan(
        tenant_id,
        case_id,
        PublicSourcingPlanCommand(
            plan_id=SourcingPlanId(new_id("spl")),
            case_id=case_id,
            target_countries=("US",),
            product_category="hinges",
            queries=(
                PublicSourcingQuery(
                    query_text="hinge factory US", target_country="US"
                ),
            ),
            max_search_queries=1,
            max_pages_read=1,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=10,
            worst_case_credits=1,
            version=1,
            expected_case_version=6,
        ),
        actor=boss,
    )
    await sourcing.confirm_public_plan(
        tenant_id, plan.plan_id, plan.plan_hash, actor=boss
    )
    candidate_id = await sourcing.submit_candidate(
        tenant_id,
        case_id,
        _candidate_submission(artifact_id),
        actor=reviewer,
    )
    verified = await sourcing.mark_candidates_verified(
        tenant_id, case_id, (candidate_id,), actor=sourcing_actor
    )
    bridge = _VerifiedBridge(
        VerifyPublicCandidateDraftsResult(
            calibration_draft_ids=(),
            converted_candidate_ids=(candidate_id,),
            rejected_candidate_ids=(),
            qualified_candidate_ids=(candidate_id,),
            verified_event=verified,
        )
    )

    engine = PostgresWorkflowEngine(
        factory,
        {
            "sourcing.verify": VerifyCandidatesStep(
                sourcing=bridge, actor=sourcing_actor
            ),
            "sourcing.prepare": PrepareCandidatesStep(
                sourcing=bridge, sourcing_actor=sourcing_actor
            ),
            "sourcing.projected": AwaitProductCardsStep(),
            "sourcing.review_wait": _AwaitReview(),
            "sourcing.handoff_retry": _HandoffRetry(),
        },
        now=lambda: NOW,
    )
    engine.register(
        WorkflowDefinition(
            workflow_type="sourcing_case",
            version=2,
            steps=(
                StepDefinition("verify_candidates", "sourcing.verify"),
                StepDefinition("prepare_candidates", "sourcing.prepare"),
                StepDefinition(
                        "await_product_cards",
                        "sourcing.projected",
                        wait_event_type="SourcingProductCardsPrepared",
                        run_on_entry=True,
                ),
                StepDefinition(
                    "await_review",
                    "sourcing.review_wait",
                    wait_event_type="SourcingReviewSubmitted",
                    run_on_entry=True,
                ),
                StepDefinition(
                    "handoff_costing",
                    "sourcing.handoff_retry",
                    wait_event_type="SourcingHandoffRetryRequested",
                ),
            ),
            transitions={
                "verify_candidates": ("prepare_candidates",),
                "prepare_candidates": ("await_product_cards",),
                "await_product_cards": ("await_review",),
                "await_review": ("handoff_costing",),
                "handoff_costing": (),
            },
        )
    )
    run_id = await engine.start(
        tenant_id,
        "sourcing_case",
        str(case_id),
        {
            "case_id": str(case_id),
            "need_id": str(need_id),
            "need_snapshot_hash": "a" * 64,
            "internal_product_ids": [],
            "supplier_candidate_ids": [],
            "sourcing_plan_id": str(plan.plan_id),
            "sourcing_plan_hash": plan.plan_hash,
            "supplier_candidate_draft_ids": ["scd-projection"],
        },
        f"projection:{case_id}",
    )
    assert await engine.poll_due(tenant_id, 1) == 1
    assert await engine.poll_due(tenant_id, 1) == 1
    pending_cards = await engine.get_run(tenant_id, RunId(run_id))
    assert (
        pending_cards is not None
        and pending_cards.current_step == "await_product_cards"
        and pending_cards.status is StepStatus.RUNNING
    )
    assert pending_cards.context["supplier_candidate_ids"] == [str(candidate_id)]
    assert pending_cards.context["candidate_case_version"] == verified.case_version
    assert pending_cards.context["candidate_set_hash"] == verified.candidate_set_hash
    assert bridge.calls == 1
    projector = SourcingCandidateProductProjector(
        sourcing=sourcing,
        products=products,
        engine=engine,
        tenant_id=tenant_id,
        sourcing_actor=sourcing_actor,
        product_actor=product_actor,
    )

    with pytest.raises(TransientError, match="尚未进入产品卡等待边界") as caught:
        await projector.handle(verified)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(
                    OutboxEventRow.tenant_id == tenant_id,
                    OutboxEventRow.event_type == "SourcingCandidatesReady",
                )
            )
            == 1
        )

    assert await engine.poll_due(tenant_id, 1) == 1
    await projector.handle(verified)
    await projector.handle(verified)

    pending_review = await engine.get_run(tenant_id, RunId(run_id))
    assert (
        pending_review is not None
        and pending_review.current_step == "await_review"
        and pending_review.status is StepStatus.RUNNING
    )
    assert len(pending_review.context["__wf_delivered_events"]) == 1
    async with factory() as session:
        product_ids = tuple(
            await session.scalars(
                select(ProductRow.product_id).where(ProductRow.tenant_id == tenant_id)
            )
        )
        option_ids = tuple(
            await session.scalars(
                select(SourcingSupplyOptionRow.option_id).where(
                    SourcingSupplyOptionRow.tenant_id == tenant_id,
                    SourcingSupplyOptionRow.case_id == case_id,
                )
            )
        )
        assert len(product_ids) == len(option_ids) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ProductCandidateSourceRow)
                .where(ProductCandidateSourceRow.tenant_id == tenant_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(
                    OutboxEventRow.tenant_id == tenant_id,
                    OutboxEventRow.event_type == "SourcingCandidatesReady",
                )
            )
            == 1
        )
    payload = {
        "case_id": str(case_id),
        "candidate_ids": [str(candidate_id)],
        "product_ids": list(product_ids),
        "option_ids": list(option_ids),
        "case_version": verified.case_version,
        "candidate_set_hash": verified.candidate_set_hash,
    }
    assert await engine.has_delivered_event(
        tenant_id,
        "sourcing_case",
        str(case_id),
        "SourcingProductCardsPrepared",
        payload,
        workflow_version=2,
        required_context={"case_id": str(case_id)},
    )

    async with factory() as session:
        case_version = await session.scalar(
            select(SourcingCaseRow.version).where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
            )
        )
    assert isinstance(case_version, int)
    application = SourcingCaseApplication(
        sourcing=sourcing, quota=_UnusedQuota(), engine=engine
    )
    review_command = SourcingReviewCommand(
        primary_option_id=option_ids[0],
        alternate_option_ids=(),
        reason="证据最完整",
        expected_case_version=case_version,
    )
    with pytest.raises(
        SourcingPlanDeliveryError, match="寻源审核工作流尚未进入等待边界"
    ) as caught:
        await application.review(
            tenant_id,
            case_id,
            review_command,
            request_id="review-request-1",
            actor=reviewer,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SourcingReviewRow)
                .where(SourcingReviewRow.tenant_id == tenant_id)
            )
            == 1
        )

    assert await engine.poll_due(tenant_id, 1) == 1
    review = await application.review(
        tenant_id,
        case_id,
        review_command,
        request_id="review-request-1",
        actor=reviewer,
    )
    replayed = await application.review(
        tenant_id,
        case_id,
        review_command,
        request_id="review-request-1",
        actor=reviewer,
    )
    retried = await application.review(
        tenant_id,
        case_id,
        review_command,
        request_id="handoff-retry-2",
        actor=reviewer,
    )

    assert review == replayed == retried
    run = await engine.get_run(tenant_id, RunId(run_id))
    assert run is not None
    assert run.current_step == "handoff_costing"
    assert run.context["sourcing_stop_reason"] == "opportunity_required"
    delivered_fingerprints = run.context["__wf_delivered_events"]
    assert len(delivered_fingerprints) == len(set(delivered_fingerprints)) == 3
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SourcingReviewRow)
                .where(SourcingReviewRow.tenant_id == tenant_id)
            )
            == 1
        )
    review_payload = {
        "review_id": str(review.review_id),
        "request_id": "review-request-1",
    }
    retry_payload = {
        "review_id": str(review.review_id),
        "request_id": "handoff-retry-2",
    }
    assert await engine.has_delivered_event(
        tenant_id,
        "sourcing_case",
        str(case_id),
        "SourcingReviewSubmitted",
        review_payload,
        workflow_version=2,
        required_context={"case_id": str(case_id)},
    )
    assert await engine.has_delivered_event(
        tenant_id,
        "sourcing_case",
        str(case_id),
        "SourcingHandoffRetryRequested",
        retry_payload,
        workflow_version=2,
        required_context={"case_id": str(case_id)},
    )
