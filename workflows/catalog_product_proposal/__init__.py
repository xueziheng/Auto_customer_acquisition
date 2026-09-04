"""目录产品提案工作流公共入口。"""

from workflows.catalog_product_proposal.application import CatalogProductApplication
from workflows.catalog_product_proposal.evaluation_flow import (
    CATALOG_EVALUATION_WORKFLOW_TYPE,
    build_catalog_evaluation_workflow_definition,
    build_catalog_evaluation_workflow_handlers,
    catalog_evaluation_idempotency_key,
    evaluation_workflow_context,
)
from workflows.catalog_product_proposal.policy_flow import (
    CATALOG_POLICY_WORKFLOW_TYPE,
    CatalogPolicyApprovalDecidedHandler,
    build_catalog_policy_workflow_definition,
    build_catalog_policy_workflow_handlers,
    catalog_policy_change_idempotency_key,
    policy_workflow_context,
    register_catalog_policy_workflow,
)
from workflows.catalog_product_proposal.proposal_flow import (
    CATALOG_CULTIVATION_WORKFLOW_TYPE,
    CatalogCultivationApprovalDecidedHandler,
    build_catalog_product_workflow_definition,
    build_catalog_product_workflow_handlers,
    catalog_cultivation_idempotency_key,
    cultivation_workflow_context,
)

__all__ = (
    "CATALOG_CULTIVATION_WORKFLOW_TYPE",
    "CATALOG_EVALUATION_WORKFLOW_TYPE",
    "CATALOG_POLICY_WORKFLOW_TYPE",
    "CatalogCultivationApprovalDecidedHandler",
    "CatalogPolicyApprovalDecidedHandler",
    "CatalogProductApplication",
    "build_catalog_evaluation_workflow_definition",
    "build_catalog_evaluation_workflow_handlers",
    "build_catalog_policy_workflow_definition",
    "build_catalog_policy_workflow_handlers",
    "build_catalog_product_workflow_definition",
    "build_catalog_product_workflow_handlers",
    "catalog_cultivation_idempotency_key",
    "catalog_evaluation_idempotency_key",
    "catalog_policy_change_idempotency_key",
    "cultivation_workflow_context",
    "evaluation_workflow_context",
    "policy_workflow_context",
    "register_catalog_policy_workflow",
)
