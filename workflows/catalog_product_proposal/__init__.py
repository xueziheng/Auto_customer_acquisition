"""目录产品提案工作流公共入口。"""

from workflows.catalog_product_proposal.policy_flow import (
    CATALOG_POLICY_WORKFLOW_TYPE,
    CatalogPolicyApprovalDecidedHandler,
    build_catalog_policy_workflow_definition,
    build_catalog_policy_workflow_handlers,
    catalog_policy_change_idempotency_key,
    policy_workflow_context,
    register_catalog_policy_workflow,
)

__all__ = (
    "CATALOG_POLICY_WORKFLOW_TYPE",
    "CatalogPolicyApprovalDecidedHandler",
    "build_catalog_policy_workflow_definition",
    "build_catalog_policy_workflow_handlers",
    "catalog_policy_change_idempotency_key",
    "policy_workflow_context",
    "register_catalog_policy_workflow",
)
