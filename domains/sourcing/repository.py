"""寻源域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.sourcing.models import (
    CaseState,
    LadderCheck,
    PublicSourcingPlan,
    SourcingCase,
    SourcingReview,
    SourcingSearchExecution,
    SourcingSearchReconciliation,
    SourcingSupplyOption,
    SupplierCandidate,
)
from domains.sourcing.schemas import SourcingHandoffSnapshot
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)


@runtime_checkable
class SourcingCaseRepository(Protocol):
    async def get_or_create(
        self, tenant_id: TenantId, case: SourcingCase
    ) -> tuple[SourcingCase, bool]:
        """原子返回 trigger key 的 canonical Case 与是否由本事务创建。"""
        ...

    async def add(self, tenant_id: TenantId, case: SourcingCase) -> None:
        """新增同租户案例；实现必须校验实体 tenant_id 一致。"""
        ...

    async def get(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> SourcingCase | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> SourcingCase | None:
        """锁定单个同租户 Case，串行化跨聚合的 canonical Option 准备。"""
        ...

    async def update(self, tenant_id: TenantId, case: SourcingCase) -> None:
        """按实体版本条件更新，失败返回并发冲突而非覆盖。"""
        ...

    async def find_active_for_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingCase | None:
        """查该需求的活跃案例（幂等入口）。"""
        ...

    async def get_by_trigger(
        self, tenant_id: TenantId, trigger_key: str
    ) -> SourcingCase | None:
        """按稳定业务触发键读取任意状态案例，保证终态后仍然幂等。"""
        ...

    async def list_by_state(
        self, tenant_id: TenantId, state: CaseState, limit: int
    ) -> list[SourcingCase]: ...


@runtime_checkable
class CandidateRepository(Protocol):
    async def add(
        self, tenant_id: TenantId, candidate: SupplierCandidate
    ) -> None:
        """保存候选。**被拒的候选也要存**——它们是核验规则的校准
        数据，没有被拒样本就无法评估规则是否过严或过松。"""
        ...

    async def get(
        self, tenant_id: TenantId, candidate_id: SupplierCandidateId
    ) -> SupplierCandidate | None: ...

    async def update(
        self, tenant_id: TenantId, candidate: SupplierCandidate
    ) -> None: ...

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId, include_rejected: bool
    ) -> list[SupplierCandidate]: ...

    async def count_qualified(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> int:
        """合格候选数，与 ``MAX_QUALIFIED_CANDIDATES`` 比较用。"""
        ...


@runtime_checkable
class PublicSourcingPlanRepository(Protocol):
    """版本化公开寻源计划存储接口。"""

    async def add(
        self, tenant_id: TenantId, plan: PublicSourcingPlan
    ) -> None: ...

    async def get(
        self, tenant_id: TenantId, plan_id: SourcingPlanId
    ) -> PublicSourcingPlan | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, plan_id: SourcingPlanId
    ) -> PublicSourcingPlan | None:
        """锁定同租户计划，串行化 authorize→running 的精确重放。"""
        ...

    async def update(
        self, tenant_id: TenantId, plan: PublicSourcingPlan
    ) -> None:
        """按版本与当前哈希更新；确认事实不可被范围改写覆盖。"""
        ...

    async def get_active_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> PublicSourcingPlan | None: ...


@runtime_checkable
class SupplyOptionRepository(Protocol):
    """现有产品与候选产品的统一供给选项存储接口。"""

    async def add(
        self, tenant_id: TenantId, option: SourcingSupplyOption
    ) -> None: ...

    async def get_or_create_supplier_candidate(
        self, tenant_id: TenantId, option: SourcingSupplyOption
    ) -> tuple[SourcingSupplyOption, bool]:
        """按 tenant+Case+Supplier Candidate 原子返回 canonical Option。"""
        ...

    async def get_or_create_existing_product(
        self, tenant_id: TenantId, option: SourcingSupplyOption
    ) -> tuple[SourcingSupplyOption, bool]:
        """按 tenant+Case+Product 原子返回现有产品 canonical Option。"""
        ...

    async def get(
        self, tenant_id: TenantId, option_id: SourcingSupplyOptionId
    ) -> SourcingSupplyOption | None: ...

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> list[SourcingSupplyOption]: ...


@runtime_checkable
class SourcingReviewRepository(Protocol):
    """审核事实存储接口；实现用 Case 版本条件写拒绝并发过期。"""

    async def add(self, tenant_id: TenantId, review: SourcingReview) -> None: ...

    async def get(
        self, tenant_id: TenantId, review_id: SourcingReviewId
    ) -> SourcingReview | None: ...

    async def get_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> SourcingReview | None: ...

    async def update(
        self, tenant_id: TenantId, review: SourcingReview
    ) -> None: ...


@runtime_checkable
class SourcingHandoffRepository(Protocol):
    """成本域读取的租户绑定、版本冻结交接快照接口。"""

    async def get_snapshot(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        review_id: SourcingReviewId,
    ) -> SourcingHandoffSnapshot | None: ...


@runtime_checkable
class LadderCheckRepository(Protocol):
    """不可变阶梯检查事实存储。"""

    async def add(self, tenant_id: TenantId, check: LadderCheck) -> None: ...

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> list[LadderCheck]: ...


@runtime_checkable
class SourcingSearchExecutionRepository(Protocol):
    """稳定请求键绑定的搜索回执存储。"""

    async def add(
        self, tenant_id: TenantId, execution: SourcingSearchExecution
    ) -> None: ...

    async def get_by_request_key(
        self, tenant_id: TenantId, request_key: str
    ) -> SourcingSearchExecution | None: ...

    async def update(
        self, tenant_id: TenantId, execution: SourcingSearchExecution
    ) -> None: ...


@runtime_checkable
class SourcingSearchReconciliationRepository(Protocol):
    """只增人工核对事实存储。"""

    async def get_or_create_canonical(
        self, tenant_id: TenantId, reconciliation: SourcingSearchReconciliation
    ) -> SourcingSearchReconciliation:
        """按操作 ID 与 execution 唯一键原子返回精确 canonical 事实；漂移冲突。"""
        ...

    async def add(
        self, tenant_id: TenantId, reconciliation: SourcingSearchReconciliation
    ) -> None: ...

    async def get_for_execution(
        self, tenant_id: TenantId, execution_id: str
    ) -> SourcingSearchReconciliation | None: ...


@runtime_checkable
class SourcingUnitOfWork(Protocol):
    """寻源聚合与 Outbox 共事务边界。"""

    cases: SourcingCaseRepository
    checks: LadderCheckRepository
    plans: PublicSourcingPlanRepository
    candidates: CandidateRepository
    options: SupplyOptionRepository
    reviews: SourcingReviewRepository
    handoffs: SourcingHandoffRepository
    search_executions: SourcingSearchExecutionRepository
    reconciliations: SourcingSearchReconciliationRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
