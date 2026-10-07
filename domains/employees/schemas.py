"""员工域对外 DTO —— 公共 View，供 apps/其他域消费，不暴露内部 models。

内部实体（``models.py``）只在本域内使用；跨域/API 一律经本文件 View。
View 全部 frozen dataclass，字段取自已实现实体，只裁剪不增业务规则。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TeamId,
    TenantId,
    UserId,
)


@dataclass(frozen=True)
class EmployeeView:
    """员工公共视图。``role`` 用字符串（不引内部 ``Role`` 枚举）。"""

    employee_id: EmployeeId
    tenant_id: TenantId
    name: str
    role: str
    user_id: UserId | None = None
    team_id: TeamId | None = None
    manager_id: EmployeeId | None = None
    languages: list[str] = field(default_factory=list)
    timezone: str | None = None
    is_active: bool = True
    max_active_accounts: int | None = None


@dataclass(frozen=True)
class OwnershipLockView:
    """归属锁公共视图：owner 与命中规则（可解释性）。"""

    tenant_id: TenantId
    account_id: ProspectAccountId
    owner: EmployeeId
    locked_at: datetime
    locked_by_rule: str


@dataclass(frozen=True)
class TerritoryAssignmentView:
    """Territory Matrix 规则公共视图。"""

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
