"""报价准备依据的只增存储契约；不涉及报价冻结或审批。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from domains.costing.schemas import (
    CostCoverageView,
    PriceEvidenceView,
    PricingPolicyView,
    QuoteFxView,
)
from shared.schemas.identifiers import CostSheetId, TenantId


@dataclass(frozen=True)
class EvidenceRecord[T]:
    """业务视图与持久幂等身份；请求 hash 包括实际确认人。"""

    tenant_id: TenantId
    record_id: str
    idempotency_key: str
    request_hash: str
    value: T


class EvidenceRepository[T](Protocol):
    """同租户只增仓储，锁定幂等键涵盖记录尚不存在的情况。"""

    async def add(self, record: EvidenceRecord[T]) -> None:
        """只追加完整且一致的人工确认记录。"""
        ...

    async def get(
        self, tenant_id: TenantId, record_id: str
    ) -> EvidenceRecord[T] | None:
        """按租户读取记录；不返回其他租户内容。"""
        ...

    async def get_for_update(
        self, tenant_id: TenantId, record_id: str
    ) -> EvidenceRecord[T] | None:
        """在当前事务锁定记录，供后续冻结读取。"""
        ...

    async def get_by_key_for_update(
        self, tenant_id: TenantId, key: str
    ) -> EvidenceRecord[T] | None:
        """串行同租户同键确认，避免并发创建两份事实。"""
        ...


class PricingPolicyRepository(EvidenceRepository[PricingPolicyView], Protocol):
    async def lock_selection(self, tenant_id: TenantId, *, exclusive: bool) -> None:
        """确认独占、freeze共享，同tenant政策集合选择与插入串行化。"""
        ...

    async def get_effective(
        self, tenant_id: TenantId, category: str | None, at: datetime
    ) -> EvidenceRecord[PricingPolicyView] | None:
        """当前已确认政策优先精确品类，再回退明确的全局政策。"""
        ...


class PriceEvidenceRepository(EvidenceRepository[PriceEvidenceView], Protocol):
    """供应商单价与实际费用保留各自事实语义。"""


class CostCoverageRepository(EvidenceRepository[CostCoverageView], Protocol):
    """完整性确认按内容 hash 定位，过期清单不会自动更新。"""

    async def get_for_sheet_hash(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId, sheet_hash: str
    ) -> EvidenceRecord[CostCoverageView] | None:
        """只按精确内容身份选最近confirmed_at/coverage_id，不回退旧清单。"""
        ...


class QuoteFxRepository(EvidenceRepository[QuoteFxView], Protocol):
    """独立保存核算到报价币种的已确认汇率。"""
