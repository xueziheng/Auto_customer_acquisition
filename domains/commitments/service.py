"""承诺域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.commitments.models import Commitment
from domains.commitments.schemas import CommitmentView
from shared.schemas.identifiers import CommitmentId, EmployeeId, TenantId


@runtime_checkable
class CommitmentService(Protocol):
    """承诺服务。"""

    async def record_extracted(
        self, tenant_id: TenantId, commitment: Commitment
    ) -> CommitmentId:
        """落提取结果（未确认态）。

        - 相对时间必须已解析为绝对时间；解析没把握则
          ``due_at_uncertain=True``
        - 同一消息同一动作重复提取返回既有 ID（幂等）
        - 发布 ``CommitmentCreated``
        """
        ...

    async def confirm(
        self, tenant_id: TenantId, commitment_id: CommitmentId,
        confirmed_by: EmployeeId, corrected_due_at: str | None = None,
    ) -> None:
        """员工确认（可顺带修正到期时间）。确认后才进提醒管道——
        模型会把客套话当承诺，未确认的提醒是噪音。"""
        ...

    async def fulfill(
        self,
        tenant_id: TenantId,
        commitment_id: CommitmentId,
        fulfilled_by: EmployeeId,
    ) -> None: ...

    async def scan_overdue(self, tenant_id: TenantId) -> int:
        """扫描逾期（scheduler_worker 定时调），返回新逾期条数。

        - 员工承诺逾期 → 提醒本人；已提醒过仍逾期 → 升级经理
          （``escalated_at`` 防重复升级）
        - 客户承诺到期 → 提醒负责员工跟进
        - 发布 ``CommitmentOverdue``
        """
        ...

    async def list_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, include_fulfilled: bool
    ) -> list[CommitmentView]:
        """某员工相关的承诺（他承诺的 + 他负责跟进的客户承诺）。
        老板问「李四有哪些承诺明天到期」走这里。"""
        ...

    async def list_overdue_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> list[CommitmentView]:
        """列出负责人当前仍逾期的承诺，按到期时间排序。"""
        ...
