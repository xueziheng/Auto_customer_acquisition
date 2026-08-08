"""员工域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.employees.models import Employee, OwnershipLock, TerritoryAssignment
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId


@runtime_checkable
class EmployeeService(Protocol):
    """员工服务。"""

    async def resolve_owner(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        *,
        country: str,
        need_category: str | None = None,
        buyer_type: str | None = None,
        language: str | None = None,
        boss_override: EmployeeId | None = None,
    ) -> OwnershipLock:
        """解析客户归属并上锁。**分配的唯一入口。**

        实现要求：
        - 按 AGENTS.md 的八级优先顺序逐级匹配，**第一个命中即停**
          （字典序规则，不是打分——分配必须可解释）
        - ``locked_by_rule`` 记录命中的是哪一级
        - 已有锁时直接返回既有锁（幂等；换负责人走 ``transfer``）
        - 全部不中 → 进经理待分配池（兜底是「等人领」不是「随机分」）
        - 上锁必须原子：两个并发分配同一企业只能有一个成功
        """
        ...

    async def transfer(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        new_owner: EmployeeId,
        transferred_by: EmployeeId,
        reason: str,
    ) -> None:
        """转移归属（「把这个客户交给王经理」）。

        必须留转移记录（谁、何时、为什么）——归属历史是复盘
        「这个客户为什么丢了」的依据之一。
        """
        ...

    async def get_ownership(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> OwnershipLock | None: ...

    async def apply_territory_from_directive(
        self, tenant_id: TenantId, assignments: dict[str, str]
    ) -> None:
        """应用指令中的市场分配段（``DirectiveActivated`` 处理器调）。

        「美国交给张三」→ 张三新增一条 country=US 的高优先级规则。
        **不回溯改已有归属锁**——指令改的是未来分配，已锁客户不动，
        除非老板显式 transfer。
        """
        ...

    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> Employee: ...

    async def list_active(self, tenant_id: TenantId) -> list[Employee]: ...

    async def list_assignments(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> list[TerritoryAssignment]: ...
