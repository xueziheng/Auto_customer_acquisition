"""成本scope与创建操作专用持久端口，不建立通用事务框架。"""

from datetime import datetime
from typing import Literal, Protocol

from domains.costing.freeze_schemas import (
    CostScopeConfirmationView,
    FrozenCostBasis,
    StoredCostScope,
)
from domains.costing.repository import CostingUnitOfWork
from shared.schemas.identifiers import CostSheetId, QuoteId, TenantId
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationOperationView,
)


class CostingFreezeRepository(Protocol):
    """全部查询与写入绑定tenant且在调用UoW的同session内执行。"""

    async def lock_key(
        self, tenant_id: TenantId, kind: Literal["scope", "creation"], key: str
    ) -> None:
        """咨询事务锁覆盖行尚未存在的并发窗口。"""
        ...

    async def get_scope_by_key(
        self, tenant_id: TenantId, key: str
    ) -> StoredCostScope | None:
        """读取永久scope幂等绑定，不回写旧确认。"""
        ...

    async def get_scope(
        self, tenant_id: TenantId, confirmation_id: str
    ) -> CostScopeConfirmationView | None:
        """读取同租户完整scope并严格校验列与payload。"""
        ...

    async def add_scope(self, tenant_id: TenantId, record: StoredCostScope) -> None:
        """追加人工适用性，不自动重确认旧清单。"""
        ...

    async def get_operation_by_key(
        self, tenant_id: TenantId, key: str
    ) -> QuoteCreationOperationView | None:
        """按原键恢复创建意图和唯一首次回执。"""
        ...

    async def get_operation(
        self, tenant_id: TenantId, operation_id: str
    ) -> QuoteCreationOperationView | None:
        """按操作ID读取完整绑定，不跨域读取报价表。"""
        ...

    async def pending_for_sheet(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> QuoteCreationOperationView | None:
        """同tenant/sheet至多一个frozen操作，存在即不能换键绕过。"""
        ...

    async def completed_for_revision(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        quote_id: QuoteId,
        quote_version: int,
    ) -> QuoteCreationOperationView | None:
        """精确读取已验证完成回执；多命中或绑定损坏必须拒绝。"""
        ...

    async def get_basis(
        self, tenant_id: TenantId, basis_id: str
    ) -> FrozenCostBasis | None:
        """读取完整不可变成本、Need、scope、核算与报价FX快照。"""
        ...

    async def add_frozen(
        self,
        tenant_id: TenantId,
        basis: FrozenCostBasis,
        operation: QuoteCreationOperationView,
    ) -> None:
        """同事务写入basis和operation，双向FK只延迟检查不关闭。"""
        ...

    async def complete(
        self,
        tenant_id: TenantId,
        operation_id: str,
        receipt: QuoteCreationCompletion,
        at: datetime,
    ) -> None:
        """只允许首次完成；同回执重放无写效果，异回执拒绝。"""
        ...

    async def mark_sheet_locked_once(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId, at: datetime
    ) -> None:
        """已持sheet行锁后仅首次窄写locked_at，不重写明细或FX。"""
        ...


class CostingFreezeUow(CostingUnitOfWork, Protocol):
    """复用原成本仓储并增加本切片只增记录仓储。"""

    freezes: CostingFreezeRepository


class CostingFreezeUowFactory(Protocol):
    """构造绑定单租户的报价冻结事务。"""

    def __call__(self, tenant_id: TenantId) -> CostingFreezeUow:
        """创建一个同session的显式租户事务。"""
        ...
