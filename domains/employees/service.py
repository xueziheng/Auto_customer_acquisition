"""员工域服务 —— **本域的公共 API**。（浅域）

所有公开读写方法都带 ``actor``（操作身份）并须经注入的
``EmployeeAuthorizer`` 判权（默认拒绝未知 action），判权通过后写
授权审计（仅 actor/action/tenant/scope/rule）。返回类型一律是
``schemas.py`` 的公共 View，**不暴露内部 models**。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.employees.permissions import Actor
from domains.employees.schemas import (
    EmployeeView,
    OwnershipLockView,
    TerritoryAssignmentView,
)
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId


@runtime_checkable
class EmployeeService(Protocol):
    """员工服务。"""

    async def resolve_owner(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        *,
        actor: Actor,
        country: str,
        need_category: str | None = None,
        buyer_type: str | None = None,
        language: str | None = None,
        timezone: str | None = None,
        boss_override: EmployeeId | None = None,
    ) -> OwnershipLockView:
        """解析客户归属并上锁。**分配的唯一入口。**

        实现要求：
        - 按 AGENTS.md 的八级优先顺序逐级匹配，**第一个命中即停**
          （字典序规则，不是打分——分配必须可解释）
        - ``locked_by_rule`` 记录命中的是哪一级
        - 已有锁时直接返回既有锁（幂等；换负责人走 ``transfer``）
        - 第 2 级历史负责人 = 按 ``transferred_at`` 升序历史**最后一条的 to_owner**
          （最近实际负责人）
        - 第 5 级语言与时区：有输入时不猜测，候选必须同时满足所有已提供条件
        - 第 6 级仅选 active SALES 常规池（排除当前匹配规则的 backup 与注入经理池），
          活跃客户数最少，同数按 ``employee_id`` 字典序稳定 tie-break
        - 第 7 级备用员工取当前匹配规则里 active 的 backup
        - 第 8 级兜底仅从注入经理池中选经员工仓储验证 active 且
          MANAGER/BOSS 的员工（非法/停用成员跳过），绝不随机
        - 全部不中且经理池无可分配成员 → ``NoAssignmentRuleError``（进池等人领）
        - 上锁必须原子：两个并发分配同一企业只能有一个成功
        """
        ...

    async def transfer(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        new_owner: EmployeeId,
        *,
        actor: Actor,
        transferred_by: EmployeeId,
        reason: str,
    ) -> None:
        """转移归属（「把这个客户交给王经理」）。

        必须留转移记录（谁、何时、为什么）——归属历史是复盘
        「这个客户为什么丢了」的依据之一。旧锁归档不删除。

        校验：账户无锁抛 ``NoOwnershipLockError``；``reason`` 去空白后必须非空；
        ``new_owner`` 与 ``transferred_by`` 均须在同一租户且 active，否则抛
        ``ValidationError``。换锁与追加历史由仓储**一次原子**完成，
        绝不允许出现「只换锁、历史没写」。
        """
        ...

    async def get_ownership(
        self, tenant_id: TenantId, account_id: ProspectAccountId, *, actor: Actor
    ) -> OwnershipLockView | None: ...

    async def apply_territory_from_directive(
        self, tenant_id: TenantId, assignments: dict[str, str], *, actor: Actor
    ) -> None:
        """应用指令中的市场分配段（``DirectiveActivated`` 处理器调）。

        「美国交给张三」→ 张三新增一条 country=US 的高优先级规则。
        **不回溯改已有归属锁**——指令改的是未来分配，已锁客户不动，
        除非老板显式 transfer。
        """
        ...

    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: Actor
    ) -> EmployeeView: ...

    async def list_active(
        self, tenant_id: TenantId, *, actor: Actor
    ) -> list[EmployeeView]: ...

    async def list_assignments(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: Actor
    ) -> list[TerritoryAssignmentView]: ...

    async def list_territory_matrix(
        self, tenant_id: TenantId, *, actor: Actor
    ) -> list[TerritoryAssignmentView]:
        """列出当前活跃员工的完整分配矩阵，过滤停用员工遗留规则并稳定排序。"""
        ...
