"""寻源域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.sourcing.models import (
    PriceRejectionReason,
    SpecMatchLevel,
    SupplierCandidate,
)
from domains.sourcing.schemas import CaseView, CandidateSubmission
from shared.schemas.identifiers import (
    EmployeeId,
    SourcingCaseId,
    TenantId,
    ValidatedNeedId,
)


def spec_match_level_values() -> tuple[str, ...]:
    """返回寻源规格匹配等级词表，供上层做确定性边界校验。"""
    return tuple(level.value for level in SpecMatchLevel)


def price_rejection_reason_values() -> tuple[str, ...]:
    """返回参考价拒绝原因词表，避免上层复制域内枚举。"""
    return tuple(reason.value for reason in PriceRejectionReason)


@runtime_checkable
class SourcingService(Protocol):
    """寻源服务。Phase 1 由人工操作驱动，接口对人工和自动化一致——
    这样 Phase 2 自动化时只换调用方，不换数据结构。"""

    async def open_case(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        assigned_to: EmployeeId | None = None,
    ) -> SourcingCaseId:
        """开寻源案例。

        实现要求：
        - 需求完整度必须 ≥ 3（数量明确），否则抛
          ``SourcingThresholdNotMetError``（完整度由上层从 demand 域
          查得传入）——带模糊需求问供应商拿不到可用报价，还消耗
          与供应商的信誉
        - 同一需求已有活跃案例时返回既有 ID（幂等）
        - 发布 ``SourcingCaseOpened``
        """
        ...

    async def record_ladder_check(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        checked_to_rung: int,
        findings: str,
    ) -> None:
        """记录匹配梯子的检查进度。

        进入公开寻源（第 6 级）前必须记录前五级的检查结论——
        「直接上 1688 找」跳过了成本更低、确定性更高的自有供应，
        这个约束靠这里的记录强制。
        """
        ...

    async def submit_candidate(
        self, tenant_id: TenantId, case_id: SourcingCaseId, submission: CandidateSubmission
    ) -> str:
        """提交候选供应商。

        实现要求：
        - 跑 ``passes_verification()``，未通过的候选照样保存但标记
          ``rejected`` 与原因——被拒候选是核验规则的校准数据，
          扔掉就没法评估规则是否太严或太松
        - 证据快照缺失直接拒绝提交（不是标记，是拒绝）：
          没有证据的候选事后无法对质
        - 合格候选数已达上限时拒绝新增，提示先淘汰一个
        """
        ...

    async def complete_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> None:
        """完成案例。

        至少一个合格候选才能完成；发布 ``SourcingCaseCompleted``
        （costing 域订阅后起 ESTIMATED 成本表）。
        """
        ...

    async def fail_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId, reason: str
    ) -> None:
        """案例失败。

        ``reason`` 必填。上层据此把机会标为 ``NO_SUPPLY_FOUND``——
        这是「哪些品类找不到供应」这个反馈信号的来源。
        """
        ...

    async def get_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> CaseView: ...

    async def list_open_cases(
        self, tenant_id: TenantId, limit: int = 50
    ) -> list[CaseView]:
        """待处理案例队列。

        Phase 1 按 ``opened_at`` 排序。**Phase 2 挂载点**：改为按
        需求簇规模排序——八个客户等同一种产品时，那个案例应该排最前。
        排序策略做成可替换的接口参数，不硬编码。
        """
        ...
