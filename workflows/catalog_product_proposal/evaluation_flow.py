"""目录簇确定性评估的 durable workflow 定义。"""

from __future__ import annotations

from collections.abc import Mapping

from domains.demand.service import DemandService
from domains.products.service import CatalogProposalService, ProductActor
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CatalogProposalPolicyVersionId,
    NeedClusterId,
    TenantId,
)
from workflows.catalog_product_proposal.evaluation_steps import EvaluateClusterStep
from workflows.catalog_product_proposal.mapping import evaluation_workflow_context
from workflows.engine.runner import StepDefinition, StepHandler, WorkflowDefinition

CATALOG_EVALUATION_WORKFLOW_TYPE = "catalog_cluster_evaluation"


def catalog_evaluation_idempotency_key(
    tenant_id: TenantId,
    cluster_id: NeedClusterId,
    policy_version_id: CatalogProposalPolicyVersionId,
    facts_hash: str,
) -> str:
    values = (tenant_id, cluster_id, policy_version_id)
    prefixes = ("tn", "ncl", "cpv")
    if any(
        not isinstance(value, str)
        or not value.startswith(f"{prefix}_")
        or value != value.strip()
        or len(value) > 40
        for value, prefix in zip(values, prefixes, strict=True)
    ) or (
        not isinstance(facts_hash, str)
        or len(facts_hash) != 64
        or any(character not in "0123456789abcdef" for character in facts_hash)
    ):
        raise ValidationError("目录簇评估 workflow 启动身份无效")
    return f"catalog-evaluation:{tenant_id}:{cluster_id}:{policy_version_id}:{facts_hash}"


def build_catalog_evaluation_workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=CATALOG_EVALUATION_WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("evaluate", "catalog_product_evaluation.evaluate"),
        ),
        transitions={"evaluate": ()},
    )


def build_catalog_evaluation_workflow_handlers(
    demand: DemandService,
    products: CatalogProposalService,
    system_actor: ProductActor,
) -> Mapping[str, StepHandler]:
    return {
        "catalog_product_evaluation.evaluate": EvaluateClusterStep(
            demand, products, system_actor
        )
    }


__all__ = (
    "CATALOG_EVALUATION_WORKFLOW_TYPE",
    "build_catalog_evaluation_workflow_definition",
    "build_catalog_evaluation_workflow_handlers",
    "catalog_evaluation_idempotency_key",
    "evaluation_workflow_context",
)
