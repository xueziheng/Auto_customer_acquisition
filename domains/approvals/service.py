"""审批域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from domains.approvals.catalog_contract import (
    CATALOG_CULTIVATION_NAMESPACE,
    CATALOG_CULTIVATION_WARNING,
    CATALOG_POLICY_NAMESPACE,
    CatalogApprovalActorFact,
    CatalogApprovalActorReader,
    CatalogApprovalCommand,
    CatalogApprovalContractError,
    CatalogApprovalFact,
    CatalogApprovalFactReader,
    CatalogCultivationApprovalCommand,
    CatalogEvidenceLocator,
    CatalogPolicyApprovalChange,
    CatalogPolicyApprovalCommand,
    CatalogPolicyContentFact,
    CatalogPolicyVersionFact,
    CatalogRuleResultFact,
    catalog_cultivation_request_hash,
    catalog_evidence_locator,
    catalog_package_fields,
    catalog_policy_content_hash,
    catalog_policy_request_hash,
    parse_catalog_evidence_locator,
)
from domains.approvals.models import ApprovalState, ApprovalType, BlastRadius
from domains.approvals.schemas import (
    ApprovalAccessResult,
    ApprovalFactView,
    ApprovalQuoteSubject,
    ApprovalReaderIdentity,
    ApprovalView,
    CatalogApprovalLinkState,
)
from shared.schemas.identifiers import ApprovalId, EmployeeId, RunId, TenantId


class QuoteApprovalAccess(Protocol):
    """报价专用当前授权；通过workflow适配，不直接依赖其他域。"""

    def subject(self, fact: ApprovalFactView) -> ApprovalQuoteSubject:
        """严格解码安全payload并匹配全部不可变身份。"""
        ...

    def display(self, fact: ApprovalFactView) -> dict[str, str]:
        """仅在当前read租约内投影已严格绑定的中文纯文本，不读取原件。"""
        ...

    def guard(
        self,
        subject: ApprovalQuoteSubject,
        *,
        actor_id: EmployeeId,
        action: Literal["read", "decide"],
    ) -> AbstractAsyncContextManager[ApprovalAccessResult]:
        """保持员工和机会锁到决定提交或视图投影结束。"""
        ...


def requires_approval(action_type: str) -> bool:
    """查 MUST_APPROVE 注册表。模块级纯函数——``tool_gateway`` 在
    每次高风险动作前调用，不该依赖服务实例。

    实现：``action_type`` 在 ``ApprovalType`` 枚举值中即返回 True。
    未知的 action_type **返回 True**（默认需要审批）——宁可多问一次
    人，不要让新加的动作类型静默绕过审批。
    """
    return True


@runtime_checkable
class ApprovalService(Protocol):
    """审批服务。"""

    async def read_fact(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> ApprovalFactView:
        """受信workflow事实读取，重新校验原始请求hash，不作HTTP出口。"""
        ...

    async def find_quote_fact(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalFactView | None:
        """受信workflow按精确新版引用恢复原包；校验原请求，不作HTTP出口。"""
        ...

    async def submit_catalog(self, command: CatalogApprovalCommand) -> ApprovalId:
        """提交严格 Catalog 审批；同引用仅完整原请求可跨状态复用。"""
        ...

    async def read_catalog_fact(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> CatalogApprovalFact:
        """受信 workflow 读取严格 Catalog 事实，不注册 HTTP。"""
        ...

    async def find_catalog_fact(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> CatalogApprovalFact | None:
        """按精确 Catalog 引用恢复 canonical 包；底层错误固定脱敏。"""
        ...

    async def get_for_reader(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        *,
        reader: ApprovalReaderIdentity,
    ) -> ApprovalView:
        """新包当前guard，旧包保持boss/manager门。"""
        ...

    async def get_catalog_link_state_for_reader(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        *,
        reader: ApprovalReaderIdentity,
    ) -> CatalogApprovalLinkState:
        """按当前员工事实返回 Products 联结所需的最小 Catalog 状态。"""
        ...

    async def list_for_reader(
        self, tenant_id: TenantId, *, reader: ApprovalReaderIdentity, limit: int = 50
    ) -> list[ApprovalView]:
        """新包稳定游标过滤，旧包保留原可见范围。"""
        ...

    async def submit(
        self,
        tenant_id: TenantId,
        approval_type: ApprovalType,
        title: str,
        proposed_change: dict,
        reason: str,
        blast_radius: BlastRadius,
        *,
        proposed_by_run: RunId | None = None,
        proposed_by_employee: EmployeeId | None = None,
        evidence_refs: list[str] | None = None,
        change_set_ref: str | None = None,
        owner_employee: EmployeeId | None = None,
        expires_at_limit: datetime | None = None,
    ) -> ApprovalId:
        """提交审批。

        实现要求：
        - ``blast_radius`` 必填且 ``if_approved`` / ``if_rejected``
          非空——审批人最需要的就是这两句话
        - 按类型算 ``expires_at``（``DEFAULT_VALIDITY``）
        - 通知走 ``notification_gateway``（发布事件，不直接调）
        - 同一 ``change_set_ref`` 已有 pending 审批时返回既有 ID（幂等）
        """
        ...

    async def decide(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        approved: bool,
        decided_by: EmployeeId,
        note: str | None = None,
    ) -> None:
        """做出决定。

        实现要求：
        - ``can_be_decided_by`` 为 False 时抛
          ``SelfApprovalNotAllowedError``（自批禁止）
        - 已过期的抛 ``ApprovalExpiredError``——过期只能重新提交，
          不能补批
        - 决定人角色需在该类型的审批权限内（ABAC：经理只能批
          自己辖区的）
        - 发布 ``ApprovalDecided``
        - 幂等：重复决定同一结果不报错，不同结果抛冲突
        """
        ...

    async def mark_applied(
        self, tenant_id: TenantId, approval_id: ApprovalId, idempotency_key: str
    ) -> bool:
        """标记变更已应用，返回是否为首次应用。

        消费方必须先通过目标对象自身的持久化幂等机制提交业务效果，再调用
        本方法记录完成。若目标动作无法证明重放安全，必须另行设计，不得用
        “先标记 applied”规避双重执行。
        """
        ...

    async def mark_apply_failed(
        self, tenant_id: TenantId, approval_id: ApprovalId, error: str
    ) -> None:
        """应用失败。转 APPLY_FAILED 并通知——**不自动重试到成功**，
        反复失败通常说明目标状态已变。"""
        ...

    async def expire_overdue(self, tenant_id: TenantId) -> int:
        """把过期的 pending 审批转 EXPIRED，返回条数。
        ``scheduler_worker`` 定时调。

        **超时永远不能变成同意。** 没有「默认批准」策略。
        """
        ...

    async def get(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        *,
        current_employee: EmployeeId | None = None,
    ) -> ApprovalView: ...

    async def get_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalView | None:
        """按不可变变更集读取最新审批事实，供上层安全适配。"""
        ...

    async def list_pending_for(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int = 50
    ) -> list[ApprovalView]:
        """某人的待审批队列（按 ABAC 过滤到其权限范围）。
        按 ``expires_at`` 升序——最先过期的排最前。"""
        ...


__all__ = (
    "CATALOG_CULTIVATION_NAMESPACE",
    "CATALOG_CULTIVATION_WARNING",
    "CATALOG_POLICY_NAMESPACE",
    "ApprovalService",
    "ApprovalState",
    "ApprovalType",
    "BlastRadius",
    "CatalogApprovalActorFact",
    "CatalogApprovalActorReader",
    "CatalogApprovalContractError",
    "CatalogApprovalFact",
    "CatalogApprovalFactReader",
    "CatalogApprovalLinkState",
    "CatalogCultivationApprovalCommand",
    "CatalogEvidenceLocator",
    "CatalogPolicyApprovalChange",
    "CatalogPolicyApprovalCommand",
    "CatalogPolicyContentFact",
    "CatalogPolicyVersionFact",
    "CatalogRuleResultFact",
    "QuoteApprovalAccess",
    "catalog_cultivation_request_hash",
    "catalog_evidence_locator",
    "catalog_package_fields",
    "catalog_policy_content_hash",
    "catalog_policy_request_hash",
    "parse_catalog_evidence_locator",
    "requires_approval",
)
