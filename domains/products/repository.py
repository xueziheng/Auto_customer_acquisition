"""产品域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal, Protocol, Self, runtime_checkable

from domains.products.models import (
    CatalogCultivationCase,
    CatalogProductProposal,
    CatalogProposalEvaluation,
    CatalogProposalPolicyVersion,
    Product,
    ProductCandidateSource,
    ProductPool,
    SupplyCapability,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    ProductId,
    SourcingCaseId,
    SupplierCandidateId,
    TenantId,
)

CatalogPageStream = Literal[
    "policies",
    "evaluations",
    "proposals",
    "cultivation_cases",
    "pending_policies",
    "awaiting_proposals",
]


@dataclass(frozen=True)
class CatalogPageCursor:
    """租户和查询流绑定的复合游标；时间与稳定 ID 缺一不可。"""

    tenant_id: TenantId
    stream: CatalogPageStream
    position_at: datetime
    entity_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.entity_id:
            raise ValueError("Catalog 游标租户与实体 ID 不得为空")
        if (
            self.position_at.tzinfo is None
            or self.position_at.utcoffset() != UTC.utcoffset(self.position_at)
        ):
            raise ValueError("Catalog 游标时间必须为 UTC")


@dataclass(frozen=True)
class CatalogPage[T_co]:
    items: tuple[T_co, ...]
    next_cursor: CatalogPageCursor | None


@runtime_checkable
class CatalogPolicyRepository(Protocol):
    async def lock_policy_namespace(self, tenant_id: TenantId) -> None:
        """串行化同租户策略 base 读取与生命周期转换，覆盖 active 缺行场景。"""
        ...

    async def add(
        self, tenant_id: TenantId, policy: CatalogProposalPolicyVersion
    ) -> CatalogProposalPolicyVersion: ...

    async def get(
        self, tenant_id: TenantId, policy_version_id: CatalogProposalPolicyVersionId
    ) -> CatalogProposalPolicyVersion | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, policy_version_id: CatalogProposalPolicyVersionId
    ) -> CatalogProposalPolicyVersion | None: ...

    async def get_active(
        self, tenant_id: TenantId, *, for_update: bool = False
    ) -> CatalogProposalPolicyVersion | None: ...

    async def get_by_creation_key(
        self, tenant_id: TenantId, proposed_by: EmployeeId, creation_key: str
    ) -> CatalogProposalPolicyVersion | None: ...

    async def update(
        self, tenant_id: TenantId, policy: CatalogProposalPolicyVersion
    ) -> CatalogProposalPolicyVersion: ...

    async def bind_approval(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        approval_id: ApprovalId,
        expected_request_hash: str,
    ) -> CatalogProposalPolicyVersion | None:
        """锁定策略并原子核对中央 Approval 的不可变 subject 与请求摘要。"""
        ...

    async def list_versions(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProposalPolicyVersion]: ...

    async def list_pending_reconciliation(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProposalPolicyVersion]: ...


@runtime_checkable
class CatalogEvaluationRepository(Protocol):
    async def add(
        self, tenant_id: TenantId, evaluation: CatalogProposalEvaluation
    ) -> CatalogProposalEvaluation: ...

    async def get(
        self, tenant_id: TenantId, evaluation_id: CatalogProposalEvaluationId
    ) -> CatalogProposalEvaluation | None: ...

    async def get_by_subject(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
        policy_version_id: CatalogProposalPolicyVersionId,
        facts_hash: str,
    ) -> CatalogProposalEvaluation | None: ...

    async def list_evaluations(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProposalEvaluation]: ...


@runtime_checkable
class CatalogProductProposalRepository(Protocol):
    async def add(
        self, tenant_id: TenantId, proposal: CatalogProductProposal
    ) -> CatalogProductProposal: ...

    async def get(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogProductProposal | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogProductProposal | None: ...

    async def get_by_evaluation(
        self, tenant_id: TenantId, evaluation_id: CatalogProposalEvaluationId
    ) -> CatalogProductProposal | None: ...

    async def update(
        self, tenant_id: TenantId, proposal: CatalogProductProposal
    ) -> CatalogProductProposal: ...

    async def bind_approval(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        approval_id: ApprovalId,
        expected_request_hash: str,
        bound_at: datetime,
    ) -> CatalogProductProposal | None:
        """锁定提案与中央 Approval，原子核对完整不可变 subject 后绑定。"""
        ...

    async def list_proposals(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProductProposal]: ...

    async def list_awaiting_reconciliation(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProductProposal]: ...


@runtime_checkable
class CatalogCultivationCaseRepository(Protocol):
    async def add(
        self, tenant_id: TenantId, cultivation_case: CatalogCultivationCase
    ) -> CatalogCultivationCase: ...

    async def get(
        self, tenant_id: TenantId, cultivation_case_id: CatalogCultivationCaseId
    ) -> CatalogCultivationCase | None: ...

    async def get_by_proposal(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogCultivationCase | None: ...

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> CatalogCultivationCase | None: ...

    async def list_cases(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogCultivationCase]: ...


@runtime_checkable
class CatalogProductsUnitOfWork(Protocol):
    """四个 Catalog 聚合与 Outbox 共用一个数据库事务。"""

    policies: CatalogPolicyRepository
    evaluations: CatalogEvaluationRepository
    proposals: CatalogProductProposalRepository
    cultivation_cases: CatalogCultivationCaseRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class ProductRepository(Protocol):
    async def add(self, tenant_id: TenantId, product: Product) -> None: ...

    async def get(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> Product | None: ...

    async def update(self, tenant_id: TenantId, product: Product) -> None: ...

    async def search(
        self,
        tenant_id: TenantId,
        pools: list[ProductPool],
        category: str | None,
        keywords: list[str],
        limit: int,
    ) -> list[Product]: ...


@runtime_checkable
class CapabilityRepository(Protocol):
    async def add(self, tenant_id: TenantId, capability: SupplyCapability) -> None: ...

    async def list_all(self, tenant_id: TenantId) -> list[SupplyCapability]: ...


@runtime_checkable
class ProductCandidateSourceRepository(Protocol):
    """候选产品来源与逐档 Evidence 价格的幂等聚合仓储。"""

    async def add(
        self, tenant_id: TenantId, source: ProductCandidateSource
    ) -> ProductCandidateSource: ...

    async def get_by_origin(
        self,
        tenant_id: TenantId,
        sourcing_case_id: SourcingCaseId,
        supplier_candidate_id: SupplierCandidateId,
    ) -> ProductCandidateSource | None: ...

    async def get_by_product(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> ProductCandidateSource | None:
        """按同租户产品读取来源；只供内部视图显示原始 Evidence。"""
        ...


@runtime_checkable
class ProductsUnitOfWork(Protocol):
    """产品聚合与 Outbox 共事务边界。"""

    products: ProductRepository
    capabilities: CapabilityRepository
    candidate_sources: ProductCandidateSourceRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
