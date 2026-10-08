"""产品域服务——本域面向工作流与 API 的公共契约。"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from domains.products.errors import (
    CatalogCultivationConflictError,
    CatalogEvaluationConflictError,
    CatalogPolicyApprovalConflictError,
    CatalogPolicyDecisionInvalidError,
    CatalogPolicyNotFoundError,
    CatalogPolicyStateTransitionError,
    CatalogProposalApprovalConflictError,
    CatalogProposalDecisionInvalidError,
    CatalogProposalNotFoundError,
    CatalogProposalStateTransitionError,
)
from domains.products.models import (
    CandidateStatus,
    CatalogCultivationCase,
    CatalogProductProposal,
    CatalogProductProposalState,
    CatalogProposalEvaluation,
    CatalogProposalPolicyState,
    CatalogProposalPolicyVersion,
    Product,
    ProductCustomerView,
    ProductInternalView,
    ProductMatchResult,
    ProductPool,
    ProductSalesView,
    ProductSpecComparison,
    ProductSpecFact,
    ProductSpecMatchLevel,
    ProductSpecRequirement,
    QualifiedProductMatch,
)
from domains.products.permissions import ProductActor, ProductRole
from domains.products.schemas import (
    CandidateProductCreate,
    CatalogApprovalDecisionInput,
    CatalogClusterFactsInput,
    CatalogCultivationCaseView,
    CatalogEvidenceSummaryInput,
    CatalogPolicyChangeSnapshot,
    CatalogPolicyReconciliationItem,
    CatalogPolicyReconciliationPage,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    CatalogProposalReconciliationItem,
    CatalogProposalReconciliationPage,
    CatalogProposalRuleResult,
    CatalogReconciliationCursor,
    CatalogReconciliationStream,
    ProductSupplyCardView,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    ProductId,
    RunId,
    TenantId,
)


def catalog_policy_creation_request_hash(
    content: CatalogProposalPolicyContent,
    proposed_by: EmployeeId,
    base_active_version_id: CatalogProposalPolicyVersionId | None,
) -> str:
    """计算策略创建与后续审批共同复核的规范请求摘要。

    摘要只绑定不可变业务请求：严格策略内容、服务端认证得到的提交人和
    在创建事务锁内读取的当前活动版本。原始幂等键、客户端字段和读取时间
    不进入摘要，避免把传输重放身份误当业务事实。
    """

    payload = {
        "schema_version": "catalog-policy-create-v1",
        "content": content.model_dump(mode="json"),
        "proposed_by": str(proposed_by),
        "base_active_version_id": (
            None if base_active_version_id is None else str(base_active_version_id)
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def catalog_cultivation_change_set_ref(
    proposal_id: CatalogProductProposalId,
    policy_version_id: CatalogProposalPolicyVersionId,
    facts_hash: str,
) -> str:
    """构造目录产品培养审批的唯一不可变 subject 引用。"""

    for value, prefix, field_name in (
        (proposal_id, "cpr", "目录产品提案 ID"),
        (policy_version_id, "cpv", "目录提案策略版本 ID"),
    ):
        if (
            not isinstance(value, str)
            or not value.startswith(f"{prefix}_")
            or len(value) <= len(prefix) + 1
            or len(value) > 40
            or value != value.strip()
        ):
            raise ValidationError(f"{field_name} 无效")
    if (
        not isinstance(facts_hash, str)
        or len(facts_hash) != 64
        or any(character not in "0123456789abcdef" for character in facts_hash)
    ):
        raise ValidationError("目录事实摘要无效")
    return f"catalog-cultivation:{proposal_id}:{policy_version_id}:{facts_hash}"


@runtime_checkable
class ProductService(Protocol):
    """产品服务；每个入口均以可信 actor 显式授权。"""

    async def search_for_matching(
        self,
        tenant_id: TenantId,
        category: str,
        keywords: list[str],
        required_specs: tuple[ProductSpecRequirement, ...],
        *,
        actor: ProductActor,
    ) -> ProductMatchResult:
        """匹配梯子 1–3；缺完整成本五元组只返回 finding。"""
        ...

    async def get_internal_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductInternalView: ...

    async def get_sales_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductSalesView: ...

    async def get_customer_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductCustomerView:
        """客户视图。**API 层对外接口只允许调这个方法**——
        内部视图泄漏给客户是永久性商业损伤。"""
        ...

    async def create_candidate_from_sourcing(
        self,
        tenant_id: TenantId,
        command: CandidateProductCreate,
        *,
        actor: ProductActor,
    ) -> ProductId:
        """以 Case+Candidate 幂等生成 source_only 产品卡。"""
        ...

    async def list_supply_cards(
        self,
        tenant_id: TenantId,
        *,
        actor: ProductActor,
        source_only: bool | None,
        limit: int,
    ) -> tuple[ProductSupplyCardView, ...]:
        """读取稳定有界的内部供应卡；source_only 价格始终只是 indicative。"""
        ...


@runtime_checkable
class CatalogProposalService(Protocol):
    """目录培养提案公共服务；只声明领域动作，不包含仓储或编排实现。"""

    async def create_policy_candidate(
        self,
        tenant_id: TenantId,
        content: CatalogProposalPolicyContent,
        *,
        idempotency_key: str,
        actor: ProductActor,
    ) -> CatalogProposalPolicyVersionId: ...

    async def get_active_policy(
        self, tenant_id: TenantId, *, actor: ProductActor
    ) -> CatalogProposalPolicyView | None:
        """没有活动策略时返回 None；不得生成生产默认策略。"""
        ...

    async def list_policy_versions(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogProposalPolicyView, ...]: ...

    async def list_pending_policy_reconciliation(
        self,
        tenant_id: TenantId,
        *,
        actor: ProductActor,
        limit: int,
        cursor: CatalogReconciliationCursor | None = None,
    ) -> CatalogPolicyReconciliationPage:
        """按创建时间与策略 ID 升序读取待启动审批流程的 locator。"""
        ...

    async def get_policy_change_snapshot(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        *,
        actor: ProductActor,
    ) -> CatalogPolicyChangeSnapshot: ...

    async def bind_policy_approval(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        approval_id: ApprovalId,
        request_hash: str,
        *,
        actor: ProductActor,
    ) -> CatalogProposalPolicyView: ...

    async def apply_policy_decision(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        decision: CatalogApprovalDecisionInput,
        *,
        actor: ProductActor,
    ) -> CatalogProposalPolicyView: ...

    async def evaluate_cluster(
        self,
        tenant_id: TenantId,
        facts: CatalogClusterFactsInput,
        *,
        proposed_by_run: RunId,
        actor: ProductActor,
    ) -> CatalogProposalEvaluationView: ...

    async def get_evaluation(
        self,
        tenant_id: TenantId,
        evaluation_id: CatalogProposalEvaluationId,
        *,
        actor: ProductActor,
    ) -> CatalogProposalEvaluationView: ...

    async def list_evaluations(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogProposalEvaluationView, ...]: ...

    async def get_proposal(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        *,
        actor: ProductActor,
    ) -> CatalogProductProposalView: ...

    async def list_proposals(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogProductProposalView, ...]: ...

    async def list_awaiting_proposal_reconciliation(
        self,
        tenant_id: TenantId,
        *,
        actor: ProductActor,
        limit: int,
        cursor: CatalogReconciliationCursor | None = None,
    ) -> CatalogProposalReconciliationPage:
        """按创建时间与提案 ID 升序读取待启动培养流程的 locator。"""
        ...

    async def bind_proposal_approval(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        approval_id: ApprovalId,
        request_hash: str,
        *,
        actor: ProductActor,
    ) -> CatalogProductProposalView: ...

    async def apply_cultivation_decision(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        decision: CatalogApprovalDecisionInput,
        current_facts: CatalogClusterFactsInput,
        *,
        actor: ProductActor,
    ) -> CatalogProductProposalView: ...

    async def get_cultivation_case(
        self,
        tenant_id: TenantId,
        cultivation_case_id: CatalogCultivationCaseId,
        *,
        actor: ProductActor,
    ) -> CatalogCultivationCaseView: ...

    async def list_cultivation_cases(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogCultivationCaseView, ...]: ...


__all__: tuple[str, ...] = (
    "CandidateStatus",
    "CatalogApprovalDecisionInput",
    "CatalogClusterFactsInput",
    "CatalogCultivationCase",
    "CatalogCultivationCaseView",
    "CatalogCultivationConflictError",
    "CatalogEvaluationConflictError",
    "CatalogEvidenceSummaryInput",
    "CatalogPolicyApprovalConflictError",
    "CatalogPolicyChangeSnapshot",
    "CatalogPolicyDecisionInvalidError",
    "CatalogPolicyNotFoundError",
    "CatalogPolicyReconciliationItem",
    "CatalogPolicyReconciliationPage",
    "CatalogPolicyStateTransitionError",
    "CatalogProductProposal",
    "CatalogProductProposalState",
    "CatalogProductProposalView",
    "CatalogProposalApprovalConflictError",
    "CatalogProposalDecisionInvalidError",
    "CatalogProposalEvaluation",
    "CatalogProposalEvaluationView",
    "CatalogProposalNotFoundError",
    "CatalogProposalPolicyContent",
    "CatalogProposalPolicyState",
    "CatalogProposalPolicyVersion",
    "CatalogProposalPolicyView",
    "CatalogProposalReconciliationItem",
    "CatalogProposalReconciliationPage",
    "CatalogProposalRuleResult",
    "CatalogProposalService",
    "CatalogProposalStateTransitionError",
    "CatalogReconciliationCursor",
    "CatalogReconciliationStream",
    "Product",
    "ProductActor",
    "ProductCustomerView",
    "ProductInternalView",
    "ProductMatchResult",
    "ProductPool",
    "ProductRole",
    "ProductSalesView",
    "ProductService",
    "ProductSpecComparison",
    "ProductSpecFact",
    "ProductSpecMatchLevel",
    "ProductSpecRequirement",
    "ProductSupplyCardView",
    "QualifiedProductMatch",
    "catalog_cultivation_change_set_ref",
    "catalog_policy_creation_request_hash",
)


from domains.products.knowledge_service import (
    EnterpriseKnowledgeService,
    EnterpriseKnowledgeServiceImpl,
)

__all__ += ("EnterpriseKnowledgeService", "EnterpriseKnowledgeServiceImpl")
