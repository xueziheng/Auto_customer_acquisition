"""组织域实体。（浅域：Playbook 写全，其余骨架）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shared.schemas.identifiers import TenantId
from shared.schemas.money import Money


@dataclass
class Tenant:
    """租户。Phase 1 只有一个，但生命周期字段现在就齐
    （ADR 0001：隔离预埋的一部分）。"""

    tenant_id: TenantId
    name: str
    created_at: datetime
    is_active: bool = True


@dataclass
class CompanyPlaybook:
    """公司 Playbook —— 老板提供的最小边界集合。

    几乎所有域都读它：打分门槛、Campaign 校验、tool_gateway 的
    Playbook stage。它是配置不是代码，改动不需要发版，但要走审批
    （``approvals.CAMPAIGN_BOUNDARY_CHANGE`` 类似的配置变更类型）。

    字段：
        tenant_id
        company_type:        贸易公司 / 工厂 / 混合
        excluded_categories: 不能做的产品（打分门槛 CATEGORY_ALLOWED
                             的数据源；命中即 compliance_blocked）
        sourcing_regions:    主要货源地
        excluded_countries:  不做的目标国家
        minimum_deal_value:  交易金额底线（打分门槛 VALUE_ABOVE_FLOOR
                             的数据源）
        monthly_budget_credits: 每月预算（Phase 3 接积分；Phase 1
                             仅记录与展示）
        approval_requirements: 额外的必须审批动作（叠加在
                             MUST_APPROVE 注册表之上，只能加严）
        supply_capabilities_note: 供应能力概述（探索章程的种子输入）
        updated_at, updated_by
    """

    tenant_id: TenantId
    company_type: str
    minimum_deal_value: Money
    updated_at: datetime
    excluded_categories: list[str] = field(default_factory=list)
    sourcing_regions: list[str] = field(default_factory=list)
    excluded_countries: list[str] = field(default_factory=list)
    monthly_budget_credits: int | None = None
    approval_requirements: list[str] = field(default_factory=list)
    supply_capabilities_note: str | None = None
    updated_by: str | None = None

    def is_category_allowed(self, category: str) -> bool:
        """品类是否允许。模糊匹配要保守：**疑似命中按命中处理**——
        排除清单存在的意义就是宁可错杀。"""
        raise NotImplementedError

    def is_country_allowed(self, country: str) -> bool:
        raise NotImplementedError
