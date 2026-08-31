"""Sourcing Case V2 的精确流程定义与 handler 装配。"""

from __future__ import annotations

from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.service import OpportunityService
from domains.products.service import ProductActor, ProductService
from domains.sourcing.service import SourcingActor, SourcingService
from domains.suppliers.service import SupplierActor, SupplierService
from workflows.engine.runner import StepDefinition, StepHandler, WorkflowDefinition
from workflows.sourcing_case.ports import SourcingNeedReader
from workflows.sourcing_case.steps import (
    AwaitProductCardsStep,
    AwaitPublicPlanStep,
    AwaitReviewStep,
    FixedWaitStep,
    HandoffCostingStep,
    InternalMatchLadderStep,
    PrepareCandidatesStep,
    VerifyCandidatesStep,
)

WORKFLOW_TYPE = "sourcing_case"


def build_sourcing_case_definition() -> WorkflowDefinition:
    """构造 V2 八步流程；外部搜索禁止引擎自动重试。"""

    return WorkflowDefinition(
        workflow_type=WORKFLOW_TYPE,
        version=2,
        steps=(
            StepDefinition("check_ladder", "sourcing_case.v2.check_ladder"),
            StepDefinition(
                "await_public_plan",
                "sourcing_case.v2.await_public_plan",
                wait_event_type="SourcingPlanConfirmed",
                run_on_entry=True,
            ),
            StepDefinition(
                "public_search",
                "sourcing_case.v2.public_search",
                max_retries=0,
                wait_event_type="SourcingSearchRetryRequested",
                run_on_entry=True,
            ),
            StepDefinition("verify_candidates", "sourcing_case.v2.verify_candidates"),
            StepDefinition("prepare_candidates", "sourcing_case.v2.prepare_candidates"),
            StepDefinition(
                "await_product_cards",
                "sourcing_case.v2.await_product_cards",
                wait_event_type="SourcingProductCardsPrepared",
                run_on_entry=True,
            ),
            StepDefinition(
                "await_review",
                "sourcing_case.v2.await_review",
                wait_event_type="SourcingReviewSubmitted",
                run_on_entry=True,
            ),
            StepDefinition(
                "handoff_costing",
                "sourcing_case.v2.handoff_costing",
                wait_event_type="SourcingHandoffRetryRequested",
                run_on_entry=True,
            ),
        ),
        transitions={
            "check_ladder": ("await_public_plan", "prepare_candidates"),
            "await_public_plan": ("public_search",),
            "public_search": ("verify_candidates",),
            "verify_candidates": ("prepare_candidates",),
            "prepare_candidates": ("await_product_cards",),
            "await_product_cards": ("await_review",),
            "await_review": ("handoff_costing",),
            "handoff_costing": (),
        },
    )


def build_sourcing_case_handlers(
    *,
    need_reader: SourcingNeedReader,
    products: ProductService,
    suppliers: SupplierService,
    sourcing: SourcingService,
    product_actor: ProductActor,
    supplier_actor: SupplierActor,
    sourcing_actor: SourcingActor,
    opportunities: OpportunityService,
    opportunity_actor: OpportunityActor,
    public_search_handler: StepHandler | None = None,
) -> dict[str, StepHandler]:
    """装配 Task 7 内部路径；后续步骤保持显式无副作用等待。"""

    return {
        "sourcing_case.v2.check_ladder": InternalMatchLadderStep(
            need_reader=need_reader,
            products=products,
            suppliers=suppliers,
            sourcing=sourcing,
            product_actor=product_actor,
            supplier_actor=supplier_actor,
            sourcing_actor=sourcing_actor,
        ),
        "sourcing_case.v2.await_public_plan": AwaitPublicPlanStep(),
        "sourcing_case.v2.public_search": public_search_handler
        or FixedWaitStep("public_search_dependencies_not_composed"),
        "sourcing_case.v2.verify_candidates": VerifyCandidatesStep(
            sourcing=sourcing,
            actor=sourcing_actor,
        ),
        "sourcing_case.v2.prepare_candidates": PrepareCandidatesStep(
            sourcing=sourcing,
            sourcing_actor=sourcing_actor,
        ),
        "sourcing_case.v2.await_product_cards": AwaitProductCardsStep(),
        "sourcing_case.v2.await_review": AwaitReviewStep(),
        "sourcing_case.v2.handoff_costing": HandoffCostingStep(
            opportunities=opportunities,
            sourcing=sourcing,
            opportunity_actor=opportunity_actor,
            sourcing_actor=sourcing_actor,
        ),
    }


__all__ = (
    "WORKFLOW_TYPE",
    "build_sourcing_case_definition",
    "build_sourcing_case_handlers",
)
