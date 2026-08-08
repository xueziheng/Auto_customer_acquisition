"""员工域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.employees.models import (
    Employee,
    OwnershipLock,
    OwnershipTransfer,
    TerritoryAssignment,
)
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId


@runtime_checkable
class EmployeeRepository(Protocol):
    async def get(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> Employee | None: ...

    async def add(self, employee: Employee) -> None: ...

    async def update(self, employee: Employee) -> None: ...

    async def list_active(self, tenant_id: TenantId) -> list[Employee]: ...

    async def count_active_accounts(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> int:
        """当前活跃客户数。分配优先级第 6 级（工作量）的数据源。"""
        ...


@runtime_checkable
class TerritoryRepository(Protocol):
    async def add(self, assignment: TerritoryAssignment) -> None: ...

    async def list_matching(
        self,
        tenant_id: TenantId,
        country: str,
        need_category: str | None,
        buyer_type: str | None,
    ) -> list[TerritoryAssignment]:
        """按维度查生效规则，按 priority 排序。分配解析器的输入。"""
        ...

    async def list_by_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> list[TerritoryAssignment]:
        """某员工的所有规则（``list_assignments`` 的数据源）。"""
        ...


@runtime_checkable
class OwnershipRepository(Protocol):
    async def try_lock(self, lock: OwnershipLock) -> bool:
        """尝试上锁，返回是否成功。

        **必须用数据库唯一约束实现**（tenant_id + account_id 唯一）：
        先查后插在并发下会让两个员工同时锁到同一家企业。
        """
        ...

    async def get(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> OwnershipLock | None: ...

    async def replace(
        self,
        tenant_id: TenantId,
        new_lock: OwnershipLock,
        transfer: OwnershipTransfer,
    ) -> None:
        """**原子**替换归属并追加转移历史：要么都成功，要么都失败（同一事务）。

        绝不能出现「只换锁、历史没写」或相反——归属历史是复盘素材。
        旧锁归档不删除。``transfer`` 已携带 from/to/by/at/reason。
        """
        ...

    async def list_transfers(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[OwnershipTransfer]:
        """按账户查转移历史（**时间升序**）。分配优先级第 2 级（历史负责人）的数据源。"""
        ...
