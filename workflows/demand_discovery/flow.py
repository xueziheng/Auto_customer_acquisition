"""demand_discovery 持久状态机定义与 handler 装配。"""

from __future__ import annotations

from datetime import timedelta

from domains.demand.service import DemandService
from domains.prospecting.service import ProspectingService
from workflows.demand_discovery.ports import (
    AccountDiscoveryQueue,
    DemandDiscoveryTaskReader,
    DemandIntelligenceCapability,
    WebDiscoveryPageReader,
    WebDiscoverySearcher,
)
from workflows.demand_discovery.steps import (
    ExecuteSearchStep,
    GenerateHypothesesStep,
    PlanSearchStep,
    ScoreAndQueueStep,
)
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
    WorkflowEngine,
)

WORKFLOW_TYPE = "demand_discovery"


def build_demand_discovery_definition() -> WorkflowDefinition:
    """确认边界 → 有界搜索 → 证据检查 → 账户发现排队。"""
    return WorkflowDefinition(
        workflow_type=WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("plan_search", "demand_discovery.plan_search"),
            StepDefinition(
                "execute_search",
                "demand_discovery.execute_search",
                # 搜索和页面读取不是可幂等外部动作；故障不得由引擎盲重放。
                max_retries=0,
                retry_backoff=timedelta(minutes=5),
            ),
            StepDefinition(
                "generate_hypotheses",
                "demand_discovery.generate_hypotheses",
            ),
            StepDefinition(
                "score_and_queue",
                "demand_discovery.score_and_queue",
            ),
        ),
        transitions={
            "plan_search": ("execute_search",),
            "execute_search": ("generate_hypotheses",),
            "generate_hypotheses": ("score_and_queue",),
            "score_and_queue": (),
        },
    )


def build_demand_discovery_handlers(
    *,
    task_reader: DemandDiscoveryTaskReader,
    searcher: WebDiscoverySearcher,
    page_reader: WebDiscoveryPageReader,
    capability: DemandIntelligenceCapability,
    demand: DemandService,
    prospecting: ProspectingService,
    account_queue: AccountDiscoveryQueue,
) -> dict[str, StepHandler]:
    return {
        "demand_discovery.plan_search": PlanSearchStep(task_reader),
        "demand_discovery.execute_search": ExecuteSearchStep(
            task_reader,
            searcher,
            page_reader,
            capability,
            demand,
            prospecting,
        ),
        "demand_discovery.generate_hypotheses": GenerateHypothesesStep(demand),
        "demand_discovery.score_and_queue": ScoreAndQueueStep(
            task_reader,
            demand,
            account_queue,
        ),
    }


def register_demand_discovery(engine: WorkflowEngine) -> None:
    engine.register(build_demand_discovery_definition())


__all__ = (
    "WORKFLOW_TYPE",
    "build_demand_discovery_definition",
    "build_demand_discovery_handlers",
    "register_demand_discovery",
)
