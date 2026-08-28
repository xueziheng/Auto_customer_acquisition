"""审批域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Self, runtime_checkable

from domains.approvals.models import ApprovalPackage, ApprovalState
from shared.events.bus import EventBus
from shared.schemas.identifiers import ApprovalId, EmployeeId, TenantId


@runtime_checkable
class ApprovalRepository(Protocol):
    async def lock_quote_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> None:
        """新版报价提交的tenant+change_set事务锁，不锁其他审批namespace。"""
        ...

    async def find_quote_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalPackage | None:
        """读取新namespace跨状态唯一包。"""
        ...

    async def list_quote_pending_candidates(
        self,
        tenant_id: TenantId,
        *,
        scan_started_at: datetime,
        after: tuple[datetime, ApprovalId] | None,
        limit: int,
    ) -> tuple[ApprovalPackage, ...]:
        """稳定游标含本人起草包，当前权限由专用guard筛选。"""
        ...

    async def add(self, package: ApprovalPackage) -> None: ...

    async def get(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> ApprovalPackage | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> ApprovalPackage | None: ...

    async def update(self, package: ApprovalPackage) -> None:
        """更新审批包。

        实现要求：只允许状态推进和决定字段写入；``proposed_change``、
        ``blast_radius`` 等内容字段创建后**不可变**——审批人批的是
        当时看到的内容，内容可变则审批无意义。
        """
        ...

    async def find_pending_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalPackage | None:
        """按变更集查 pending 审批（提交幂等用）。"""
        ...

    async def find_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalPackage | None:
        """读取变更集最新审批事实，供上层适配 Campaign 版本审批。"""
        ...

    async def list_pending_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int
    ) -> list[ApprovalPackage]: ...

    async def list_expired_candidates(
        self, tenant_id: TenantId, now: datetime, limit: int
    ) -> list[ApprovalPackage]: ...

    async def record_application(
        self, tenant_id: TenantId, approval_id: ApprovalId, idempotency_key: str
    ) -> bool:
        """记录应用，返回是否首次。

        幂等键唯一索引实现：插入成功 = 首次，冲突 = 重复。
        **必须用数据库约束**，不能用先查后插——并发下先查后插会
        双重应用。
        """
        ...

    async def count_by_state(
        self, tenant_id: TenantId, state: ApprovalState
    ) -> int: ...


@runtime_checkable
class ApprovalUnitOfWork(Protocol):
    approvals: ApprovalRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


@runtime_checkable
class ApprovalUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> ApprovalUnitOfWork: ...
