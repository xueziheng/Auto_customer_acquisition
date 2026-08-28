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
    ) -> StoredCostScope | None: ...
    async def get_scope(
        self, tenant_id: TenantId, confirmation_id: str
    ) -> CostScopeConfirmationView | None: ...
    async def add_scope(self, tenant_id: TenantId, record: StoredCostScope) -> None: ...
    async def get_operation_by_key(
        self, tenant_id: TenantId, key: str
    ) -> QuoteCreationOperationView | None: ...
    async def get_operation(
        self, tenant_id: TenantId, operation_id: str
    ) -> QuoteCreationOperationView | None: ...
    async def pending_for_sheet(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> QuoteCreationOperationView | None: ...
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
    ) -> FrozenCostBasis | None: ...
    async def add_frozen(
        self,
        tenant_id: TenantId,
        basis: FrozenCostBasis,
        operation: QuoteCreationOperationView,
    ) -> None: ...
    async def complete(
        self,
        tenant_id: TenantId,
        operation_id: str,
        receipt: QuoteCreationCompletion,
        at: datetime,
    ) -> None: ...
    async def mark_sheet_locked_once(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId, at: datetime
    ) -> None: ...


class CostingFreezeUow(CostingUnitOfWork, Protocol):
    """复用原成本仓储并增加本切片只增记录仓储。"""

    freezes: CostingFreezeRepository


class CostingFreezeUowFactory(Protocol):
    """构造绑定单租户的报价冻结事务。"""

    def __call__(self, tenant_id: TenantId) -> CostingFreezeUow: ...
