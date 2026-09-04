"""产品域服务——本域面向工作流与 API 的公共契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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
from domains.products.permissions import ProductActor
from domains.products.schemas import (
    CandidateProductCreate,
    CatalogApprovalDecisionInput,
    CatalogClusterFactsInput,
    CatalogCultivationCaseView,
    CatalogPolicyChangeSnapshot,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    ProductSupplyCardView,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    ProductId,
    RunId,
    TenantId,
)


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

__all__ = (
    "CandidateStatus",
    "CatalogCultivationCase",
    "CatalogCultivationCaseView",
    "CatalogProductProposal",
    "CatalogProductProposalState",
    "CatalogProductProposalView",
    "CatalogProposalEvaluation",
    "CatalogProposalEvaluationView",
    "CatalogProposalPolicyContent",
    "CatalogProposalPolicyState",
    "CatalogProposalPolicyVersion",
    "CatalogProposalPolicyView",
    "CatalogProposalService",
    "Product",
    "ProductActor",
    "ProductCustomerView",
    "ProductInternalView",
    "ProductMatchResult",
    "ProductPool",
    "ProductSalesView",
    "ProductService",
    "ProductSpecComparison",
    "ProductSpecFact",
    "ProductSpecMatchLevel",
    "ProductSpecRequirement",
    "ProductSupplyCardView",
    "QualifiedProductMatch",
)
