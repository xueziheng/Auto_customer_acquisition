"""审批域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.approvals.models import ApprovalState, ApprovalType, BlastRadius
from domains.approvals.schemas import ApprovalView
from shared.schemas.identifiers import ApprovalId, EmployeeId, RunId, TenantId


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
    "ApprovalService",
    "ApprovalState",
    "ApprovalType",
    "BlastRadius",
    "requires_approval",
)
