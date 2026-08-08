"""员工域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.employees.models import Employee, OwnershipLock, TerritoryAssignment
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
        self, tenant_id: TenantId, new_lock: OwnershipLock, reason: str
    ) -> None:
        """转移归属。旧锁归档不删除——归属历史是复盘素材。"""
        ...
