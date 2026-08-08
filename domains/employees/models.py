"""员工域实体。（浅域：Territory Matrix 与归属锁写全，其余骨架）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TeamId,
    TenantId,
    UserId,
)


class Role(str, Enum):
    """角色 —— RBAC 的数据源。权限矩阵见
    ``docs/architecture/03-permissions.md``。"""

    BOSS = "boss"
    MANAGER = "manager"
    SALES = "sales"
    SOURCING = "sourcing"
    PRODUCT = "product"
    FINANCE = "finance"
    VIEWER = "viewer"


@dataclass
class Employee:
    """员工。与登录账号（``UserId``）分离——离职员工的历史归属
    记录必须保留，账号可以停用。"""

    employee_id: EmployeeId
    tenant_id: TenantId
    name: str
    role: Role
    created_at: datetime
    user_id: UserId | None = None
    team_id: TeamId | None = None
    manager_id: EmployeeId | None = None
    languages: list[str] = field(default_factory=list)
    timezone: str | None = None
    is_active: bool = True
    max_active_accounts: int | None = None


@dataclass
class TerritoryAssignment:
    """业务分配矩阵的一条规则。

    设计稿第二十节的完整字段。规则可叠加：一个员工多条规则，
    一个国家多个员工（按需求类别细分）。

    字段：
        tenant_id, employee_id
        countries, product_categories, need_categories, buyer_types
        languages
        manager_id, backup_employee_id
        priority:       同时命中多条规则时的排序
        effective_from, effective_until
    """

    tenant_id: TenantId
    employee_id: EmployeeId
    priority: int
    effective_from: datetime
    countries: list[str] = field(default_factory=list)
    product_categories: list[str] = field(default_factory=list)
    need_categories: list[str] = field(default_factory=list)
    buyer_types: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    manager_id: EmployeeId | None = None
    backup_employee_id: EmployeeId | None = None
    effective_until: datetime | None = None


@dataclass(frozen=True)
class OwnershipLock:
    """客户归属锁。粒度是**企业**。

    同一家公司的不同联系人必须归同一员工——两个销售对同一家公司
    说出两套价格，客户直接判定管理混乱。

    字段：
        tenant_id, account_id, owner
        locked_at
        locked_by_rule:  命中了分配优先级的第几条（可解释性：
                         「为什么给了张三」要一句话答得出）
    """

    tenant_id: TenantId
    account_id: ProspectAccountId
    owner: EmployeeId
    locked_at: datetime
    locked_by_rule: str


@dataclass(frozen=True)
class OwnershipTransfer:
    """归属转移记录（**只增历史**：复盘「这个客户为什么丢了」的依据之一）。

    旧锁归档不删除；转移记录一旦写入不再修改。``from_owner`` 允许为空
    （历史数据可能没有来源），但 S3-3 服务层强制当前必须有锁才转移。
    """

    transfer_id: str
    tenant_id: TenantId
    account_id: ProspectAccountId
    from_owner: EmployeeId | None
    to_owner: EmployeeId
    transferred_by: EmployeeId
    transferred_at: datetime
    reason: str
