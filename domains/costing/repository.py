"""成本域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from domains.costing.models import CostSheet, MarginRule
from domains.costing.quote_repository import (
    CostCoverageRepository,
    CostingOpportunityReferenceReader,
    PriceEvidenceRepository,
    PricingPolicyRepository,
    QuoteFxRepository,
)
from shared.schemas.identifiers import CostSheetId, OpportunityId, TenantId


@runtime_checkable
class CostSheetRepository(Protocol):
    async def add(self, sheet: CostSheet) -> None: ...

    async def get(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> CostSheet | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> CostSheet | None:
        """锁行后读取，供追加成本项等读改写操作避免并发丢更新。"""
        ...

    async def update(self, sheet: CostSheet) -> None:
        """更新成本表。

        实现要求：``locked_at`` 非 None 的表拒绝任何更新，抛
        ``LockedCostSheetError``。**在存储层再拦一次**，不只靠服务层——
        锁定的成本表是历史报价的依据，改了它等于篡改历史。
        """
        ...

    async def next_version_number(
        self, tenant_id: TenantId, opportunity_id: OpportunityId, version_type: str
    ) -> int: ...

    async def list_by_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> list[CostSheet]: ...


@runtime_checkable
class MarginRuleRepository(Protocol):
    async def get_effective(
        self, tenant_id: TenantId, category: str | None
    ) -> MarginRule:
        """取生效的利润规则。品类规则优先于全局默认。

        **没有配置时抛错，不返回硬编码默认值**——利润底线是老板的
        商业决策，代码里不该有一个隐含的数字替他决定。
        """
        ...

    async def set_rule(self, rule: MarginRule) -> None: ...


@runtime_checkable
class CostingUnitOfWork(Protocol):
    opportunity_refs: CostingOpportunityReferenceReader
    sheets: CostSheetRepository
    margin_rules: MarginRuleRepository
    policies: PricingPolicyRepository
    prices: PriceEvidenceRepository
    coverage: CostCoverageRepository
    quote_fx: QuoteFxRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


@runtime_checkable
class CostingUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> CostingUnitOfWork: ...
