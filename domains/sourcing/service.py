"""寻源域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from domains.sourcing.models import (
    LadderCheck,
    LadderOutcome,
    MatchLadderRung,
    PriceRejectionReason,
    PublicPlanStatus,
    PublicSourcingPlan,
    SourcingReconciliationStatus,
    SourcingSearchExecution,
    SourcingSearchExecutionStatus,
    SourcingSearchReconciliation,
    SourcingReview,
    SpecComparison,
    SpecMatchLevel,
)
from domains.sourcing.permissions import SourcingActor
from domains.sourcing.schemas import (
    CandidateSubmission,
    CaseView,
    OpenSourcingCase,
    PublicSourcingPlanCommand,
    SourcingHandoffSnapshot,
    SourcingCandidateProductInputs,
    SourcingNeedSnapshot,
    SourcingReviewCommand,
    SourcingUncertainReconciliationCommand,
    VerifyPublicCandidateDraftsCommand,
    VerifyPublicCandidateDraftsResult,
)
from shared.events.catalog import SourcingCandidatesVerified
from shared.schemas.identifiers import (
    ArtifactId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)


@dataclass(frozen=True)
class CandidateEvidenceSnapshot:
    """可信 Artifact reader 返回的候选网页快照安全投影。"""

    tenant_id: TenantId
    artifact_id: ArtifactId
    canonical_url: str
    content_hash: str
    observed_at: datetime


@runtime_checkable
class CandidateEvidenceSnapshotReader(Protocol):
    """按 tenant+Artifact 读取已验证的不可变候选证据元数据。"""

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> CandidateEvidenceSnapshot:
        """未知、不可读或不安全的 Artifact 必须失败关闭。"""
        ...


@dataclass(frozen=True)
class ProviderUsageEvidenceSnapshot:
    """可信 reader 返回的 Tavily 账户用量原件安全投影。"""

    tenant_id: TenantId
    artifact_id: ArtifactId
    provider: str
    content_hash: str
    observed_at: datetime


@runtime_checkable
class ProviderUsageEvidenceReader(Protocol):
    """按 tenant+Artifact 核验不可变 Provider 账户用量原件。"""

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> ProviderUsageEvidenceSnapshot:
        """未知、跨租户、非 Tavily 用量原件或读取失败必须关闭。"""
        ...


@dataclass(frozen=True)
class PublicSourcingRunView:
    """老板运行公开计划前由域服务重建的精确授权快照。"""

    tenant_id: TenantId
    case_id: SourcingCaseId
    need_id: ValidatedNeedId
    case_version: int
    active_plan: PublicSourcingPlan


def spec_match_level_values() -> tuple[str, ...]:
    """返回寻源规格匹配等级词表，供上层做确定性边界校验。"""
    return tuple(level.value for level in SpecMatchLevel)


def price_rejection_reason_values() -> tuple[str, ...]:
    """返回参考价拒绝原因词表，避免上层复制域内枚举。"""
    return tuple(reason.value for reason in PriceRejectionReason)


@runtime_checkable
class SourcingService(Protocol):
    """V2 寻源公共服务。

    每个入口显式接收由可信身份解析器构造的 ``SourcingActor``；Task 6
    实现必须先判权再读取 Repository，不能从命令体推导 tenant 或 actor。
    """

    async def open_case(
        self,
        tenant_id: TenantId,
        command: OpenSourcingCase,
        *,
        actor: SourcingActor,
    ) -> SourcingCaseId:
        """开寻源案例。

        实现要求：
        - ``command.need.completeness`` 必须 ≥ 3（数量明确），否则抛
          ``SourcingThresholdNotMetError``——带模糊需求问供应商拿不到可用报价，还消耗
          与供应商的信誉
        - 同租户、Need、workflow version 共用 ``trigger_key``，重复入口返回既有 ID
        - 发布 ``SourcingCaseOpened``
        """
        ...

    async def record_ladder_check(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        check: LadderCheck,
        *,
        actor: SourcingActor,
    ) -> None:
        """记录匹配梯子的检查进度。

        进入公开寻源（第 6 级）前必须记录前五级的检查结论——
        「直接上 1688 找」跳过了成本更低、确定性更高的自有供应，
        这个约束靠这里的记录强制。
        """
        ...

    async def save_public_plan(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: PublicSourcingPlanCommand,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingPlan:
        """连续完成梯子 1–5 后保存精确、待确认的版本化范围。"""
        ...

    async def mark_candidates_ready(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        option_ids: tuple[SourcingSupplyOptionId, ...],
        candidate_ids: tuple[SupplierCandidateId, ...],
        *,
        expected_case_version: int | None = None,
        expected_candidate_set_hash: str | None = None,
        actor: SourcingActor,
    ) -> None:
        """冻结全部供给选项并原子发布候选就绪事实。"""
        ...

    async def mark_candidates_verified(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        candidate_ids: tuple[SupplierCandidateId, ...],
        *,
        actor: SourcingActor,
    ) -> SourcingCandidatesVerified:
        """原子封存精确候选集并返回产品卡投影 generation。"""
        ...

    async def verify_public_candidate_drafts(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: VerifyPublicCandidateDraftsCommand,
        *,
        actor: SourcingActor,
    ) -> VerifyPublicCandidateDraftsResult:
        """确定性转换精确公开草稿集合，并在有合格候选时原子封存。"""
        ...

    async def register_supplier_candidate_option(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        candidate_id: SupplierCandidateId,
        product_id: ProductId,
        *,
        expected_case_version: int | None = None,
        expected_candidate_set_hash: str | None = None,
        actor: SourcingActor,
    ) -> SourcingSupplyOptionId:
        """SYSTEM 幂等登记真实候选产品卡与供应商候选的供给选项绑定。"""
        ...

    async def get_candidate_product_inputs(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        candidate_ids: tuple[SupplierCandidateId, ...],
        *,
        expected_case_version: int,
        expected_candidate_set_hash: str,
        actor: SourcingActor,
    ) -> SourcingCandidateProductInputs:
        """从仓储重建精确封存 generation 的产品卡输入。"""
        ...

    async def register_existing_product_option(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        product_id: ProductId,
        *,
        actor: SourcingActor,
    ) -> SourcingSupplyOptionId:
        """SYSTEM 幂等登记已由合格内部梯级证明的真实产品供给选项。"""
        ...

    async def review(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: SourcingReviewCommand,
        *,
        actor: SourcingActor,
    ) -> SourcingReview:
        """保存一个主选和至多两个备选的人工审核事实。"""
        ...

    async def hand_to_costing(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        opportunity_id: OpportunityId,
        *,
        actor: SourcingActor,
    ) -> SourcingHandoffSnapshot:
        """把可信 Opportunity 与审核绑定并以单次 CAS 推进至终态。"""
        ...

    async def submit_candidate(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        submission: CandidateSubmission,
        *,
        actor: SourcingActor,
    ) -> SupplierCandidateId:
        """提交候选供应商。

        实现要求：
        - 跑 ``passes_verification()``，未通过的候选照样保存但标记
          ``rejected`` 与原因——被拒候选是核验规则的校准数据，
          扔掉就没法评估规则是否太严或太松
        - 证据快照缺失直接拒绝提交（不是标记，是拒绝）：
          没有证据的候选事后无法对质
        - 合格候选数已达上限时仍保存新候选，但以结构化原因标记 rejected
        - ``verified_by`` 必须从可信 ``actor`` 派生，命令体不得自证核验人
        """
        ...

    async def complete_case(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
    ) -> None:
        """旧完成入口仅为接口兼容保留。

        V2 必须走候选就绪、审核确认与 ``hand_to_costing``；实现应 fail closed，
        不得通过本入口绕过 P10 版本门禁。
        """
        ...

    async def fail_case(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
        reason: str,
    ) -> None:
        """以既定公共停止码结束允许失败的早期案例。

        ``reason`` 只接受 P1 精确代码；不得把 Provider 自由文本、原始错误或敏感内容
        写入公共失败事实。终态和 ``candidates_ready`` 不允许经此入口失败。
        """
        ...

    async def get_case(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
    ) -> CaseView: ...

    async def list_open_cases(
        self, tenant_id: TenantId, actor: SourcingActor, limit: int = 50
    ) -> list[CaseView]:
        """待处理案例队列。

        Phase 1 按 ``opened_at`` 排序。**Phase 2 挂载点**：改为按
        需求簇规模排序——八个客户等同一种产品时，那个案例应该排最前。
        排序策略做成可替换的接口参数，不硬编码。
        """
        ...

    async def create_public_plan(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        command: PublicSourcingPlanCommand,
        *,
        created_at: datetime,
    ) -> PublicSourcingPlan:
        """创建待确认计划；实现不得在此预留额度或执行外部搜索。"""
        ...

    async def confirm_public_plan(
        self,
        tenant_id: TenantId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
        expected_case_id: SourcingCaseId | None = None,
    ) -> PublicSourcingPlan:
        """确认精确哈希；显式 Case 必须在任何状态变更前精确绑定。"""
        ...

    async def get_public_plan_run_view(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingRunView:
        """boss-only 重建当前 Case 与精确活跃计划，不改变状态。"""
        ...

    async def authorize_public_plan_run(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingPlan:
        """boss-only 原子执行 authorized→running；精确 running 重放为 no-op。"""
        ...

    async def get_uncertain_search_execution(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        request_key: str,
        *,
        actor: SourcingActor,
    ) -> SourcingSearchExecution:
        """boss-only 重读同 Case/Run/request 的不确定搜索回执。"""
        ...

    async def record_confirmed_consumed_reconciliation(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: SourcingUncertainReconciliationCommand,
        *,
        actor: SourcingActor,
    ) -> SourcingSearchReconciliation:
        """核验证据后只增 confirmed_consumed；精确重放不重复写。"""
        ...

    async def submit_review(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
        command: SourcingReviewCommand,
    ) -> SourcingReview:
        """提交一个主选和至多两个备选；按 expected_case_version 条件写。"""
        ...

    async def confirm_review(
        self,
        tenant_id: TenantId,
        review_id: SourcingReviewId,
        *,
        actor: SourcingActor,
    ) -> SourcingReview:
        """老板逐次确认审核；不接受请求体自证确认人。"""
        ...

    async def get_handoff_snapshot(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
        review_id: SourcingReviewId,
    ) -> SourcingHandoffSnapshot:
        """读取冻结成本交接结果；只返回 indicative 价格选项。"""
        ...
