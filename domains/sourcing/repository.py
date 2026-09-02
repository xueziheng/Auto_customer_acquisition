"""寻源域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.sourcing.models import (
    AdmissionBlockedReason,
    AdmissionState,
    CaseState,
    LadderCheck,
    PublicSourcingPlan,
    SourcingAdmission,
    SourcingCase,
    SourcingPrioritySnapshot,
    SourcingReview,
    SourcingSearchExecution,
    SourcingSearchReconciliation,
    SourcingStopCode,
    SourcingStopDetail,
    SourcingSupplyOption,
    SupplierCandidate,
)
from domains.sourcing.schemas import PublicCandidateDraft, SourcingHandoffSnapshot
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    NeedClusterId,
    RunId,
    SourcingAdmissionId,
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

    async def update(
        self,
        tenant_id: TenantId,
        case: SourcingCase,
        *,
        clear_recoverable_stop: bool = False,
    ) -> None:
        """按实体版本条件更新；默认保留并发写入的可恢复等待 stop。"""
        ...

    async def set_recoverable_stop(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        *,
        expected_version: int,
        stop_code: SourcingStopCode,
        stop_detail: SourcingStopDetail,
    ) -> None:
        """仅在未停止的精确 Case revision 写入安全等待投影，不改变业务版本。"""
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
class SourcingAdmissionRepository(Protocol):
    """准入与只增优先级快照的 tenant-bound 存储契约。"""

    async def get_or_create(
        self,
        tenant_id: TenantId,
        admission: SourcingAdmission,
        initial_snapshot: SourcingPrioritySnapshot | None,
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot | None, bool]:
        """原子写入 admission 与可选首快照并绑定 current pointer。

        无快照仅允许 ``priority_facts_invalid`` blocked admission；其他状态必须
        随首快照一同 canonical 化。实现返回既有记录时也必须返回其 current snapshot
        （无快照的固定阻断记录则返回 ``None``）。
        """
        ...

    async def get(
        self, tenant_id: TenantId, admission_id: SourcingAdmissionId
    ) -> SourcingAdmission | None:
        """读取单条同租户 admission；不存在或异租户均返回 ``None``。"""
        ...

    async def get_with_current_snapshot(
        self, tenant_id: TenantId, admission_id: SourcingAdmissionId
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot | None] | None:
        """一次 tenant-bound JOIN 读取 admission 与其 current snapshot。"""
        ...

    async def append_snapshot_if_changed(
        self, tenant_id: TenantId, snapshot: SourcingPrioritySnapshot
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot, bool]:
        """同事务按 facts_hash 去重追加、推进（含空）current pointer 并返回更新后的 admission。"""
        ...

    async def list_cluster_refresh_targets(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
        changed_need_id: ValidatedNeedId,
    ) -> list[tuple[SourcingAdmission, SourcingPrioritySnapshot | None]]:
        """一次 LEFT JOIN 读取整簇可刷新项及 changed Need 的无快照阻断项。

        仅返回 ``waiting``/``blocked``，按 admission ID 稳定排序且不截断；
        ``starting``/``admitted`` 必须在 SQL 目标集之外保持不变。
        """
        ...

    async def claim_ordered(
        self,
        tenant_id: TenantId,
        limit: int,
        claim_token: str,
        claim_expires_at: datetime,
        now: datetime,
    ) -> list[SourcingAdmission]:
        """只按当前快照固定排序 claim 等待项；实现必须跳过并发已锁定记录。"""
        ...

    async def claim_one(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        claim_expires_at: datetime,
        now: datetime,
        *,
        requested_by: str,
    ) -> SourcingAdmission | None:
        """以可信人工 actor 精确 claim；既有 actor 不得被另一员工覆盖。"""
        ...

    async def complete(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        workflow_run_id: RunId,
        system_actor_id: str,
        admitted_at: datetime,
    ) -> SourcingAdmission | None:
        """匹配租约绑定 Run；审计取持久人工 actor，否则取 system actor。"""
        ...

    async def release_expired_claims(
        self, tenant_id: TenantId, now: datetime
    ) -> list[SourcingAdmission]:
        """释放到期租约，不对尚未到期的不确定执行做即时重试。"""
        ...

    async def release(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        released_at: datetime,
    ) -> SourcingAdmission | None:
        """以当前租约释放已知暂态失败，避免其他 worker 覆盖新 claim。"""
        ...

    async def block(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        reason: AdmissionBlockedReason,
        blocked_at: datetime,
        claim_token: str | None = None,
    ) -> SourcingAdmission | None:
        """阻断 waiting 或精确匹配租约的 starting 记录，不接收自由异常文本。"""
        ...

    async def list_by_state(
        self, tenant_id: TenantId, state: AdmissionState, limit: int
    ) -> list[SourcingAdmission]:
        """按 repository 的确定性顺序读取同租户状态列表。"""
        ...

    async def list_by_state_with_current_snapshot(
        self, tenant_id: TenantId, state: AdmissionState, limit: int
    ) -> list[tuple[SourcingAdmission, SourcingPrioritySnapshot | None]]:
        """按同一 repository 顺序批量读取 admission 与 current snapshot，禁止 N+1。"""
        ...


@runtime_checkable
class CandidateRepository(Protocol):
    async def add(self, tenant_id: TenantId, candidate: SupplierCandidate) -> None:
        """保存候选。**被拒的候选也要存**——它们是核验规则的校准
        数据，没有被拒样本就无法评估规则是否过严或过松。"""
        ...

    async def get(
        self, tenant_id: TenantId, candidate_id: SupplierCandidateId
    ) -> SupplierCandidate | None: ...

    async def get_or_create_public_draft(
        self, tenant_id: TenantId, candidate: SupplierCandidate
    ) -> tuple[SupplierCandidate, bool]:
        """按完整公开草稿 source key 原子返回 canonical 候选。"""
        ...

    async def get_by_public_draft_source_key(
        self, tenant_id: TenantId, source_key: str
    ) -> SupplierCandidate | None: ...

    async def update(
        self, tenant_id: TenantId, candidate: SupplierCandidate
    ) -> None: ...

    async def list_for_case(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        include_rejected: bool,
        limit: int | None = None,
    ) -> list[SupplierCandidate]: ...

    async def count_qualified(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> int:
        """合格候选数，与 ``MAX_QUALIFIED_CANDIDATES`` 比较用。"""
        ...


@runtime_checkable
class PublicSourcingPlanRepository(Protocol):
    """版本化公开寻源计划存储接口。"""

    async def add(self, tenant_id: TenantId, plan: PublicSourcingPlan) -> None: ...

    async def get(
        self, tenant_id: TenantId, plan_id: SourcingPlanId
    ) -> PublicSourcingPlan | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, plan_id: SourcingPlanId
    ) -> PublicSourcingPlan | None:
        """锁定同租户计划，串行化 authorize→running 的精确重放。"""
        ...

    async def update(self, tenant_id: TenantId, plan: PublicSourcingPlan) -> None:
        """按版本与当前哈希更新；确认事实不可被范围改写覆盖。"""
        ...

    async def get_active_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> PublicSourcingPlan | None: ...


@runtime_checkable
class SupplyOptionRepository(Protocol):
    """现有产品与候选产品的统一供给选项存储接口。"""

    async def add(self, tenant_id: TenantId, option: SourcingSupplyOption) -> None: ...

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

    async def update(self, tenant_id: TenantId, review: SourcingReview) -> None: ...


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

    async def get_or_create_canonical(
        self, tenant_id: TenantId, execution: SourcingSearchExecution
    ) -> SourcingSearchExecution: ...

    async def get_by_request_key(
        self, tenant_id: TenantId, request_key: str
    ) -> SourcingSearchExecution | None: ...

    async def list_uncertain_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId, limit: int
    ) -> list[SourcingSearchExecution]:
        """读取同租户 Case 的不确定回执；按稳定时间/操作标识有界排序。"""
        ...

    async def update(
        self, tenant_id: TenantId, execution: SourcingSearchExecution
    ) -> None: ...


@runtime_checkable
class PublicCandidateDraftRepository(Protocol):
    """未核验公开页面草稿的 tenant-bound 幂等存储。"""

    async def get_or_create_canonical(
        self, tenant_id: TenantId, draft: PublicCandidateDraft
    ) -> PublicCandidateDraft: ...

    async def get_by_source_key(
        self, tenant_id: TenantId, source_key: str
    ) -> PublicCandidateDraft | None: ...

    async def list_exact_for_verification(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        plan_id: SourcingPlanId,
        plan_hash: str,
    ) -> list[PublicCandidateDraft]:
        """按 query/result/source key 返回一次精确公开搜索的 canonical 草稿。"""
        ...


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
    admissions: SourcingAdmissionRepository
    checks: LadderCheckRepository
    plans: PublicSourcingPlanRepository
    candidates: CandidateRepository
    options: SupplyOptionRepository
    reviews: SourcingReviewRepository
    handoffs: SourcingHandoffRepository
    search_executions: SourcingSearchExecutionRepository
    candidate_drafts: PublicCandidateDraftRepository
    reconciliations: SourcingSearchReconciliationRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
