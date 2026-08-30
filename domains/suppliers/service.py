"""供应商域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Protocol, runtime_checkable

from domains.suppliers.models import Supplier, SupplierPriceRecord
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import SupplierId, TenantId


class SupplierRole(str, Enum):
    SYSTEM = "system"
    BOSS = "boss"
    PRODUCT = "product"
    SOURCING = "sourcing"
    FINANCE = "finance"


class SupplierAction(str, Enum):
    REGISTER = "register"
    PRICE_RECORD = "price_record"
    CAPABILITY_SEARCH = "capability_search"
    READ = "read"


@dataclass(frozen=True)
class SupplierActor:
    """由可信身份边界创建，不接受请求体角色字符串自证。"""

    actor_id: str
    role: SupplierRole
    tenant_id: TenantId

    def __post_init__(self) -> None:
        if not self.actor_id or self.actor_id != self.actor_id.strip():
            raise ValidationError("供应商操作身份无效")
        if not isinstance(self.role, SupplierRole):
            raise ValidationError("供应商操作角色无效")
        if not str(self.tenant_id).strip():
            raise ValidationError("供应商操作租户无效")


@runtime_checkable
class SupplierAuthorizer(Protocol):
    def require(
        self, actor: SupplierActor, action: SupplierAction, tenant_id: TenantId
    ) -> str: ...


class Phase2SupplierAuthorizer:
    """只开放内部供应网络匹配和证据价格历史所需权限。"""

    _ALLOWED: ClassVar[dict[SupplierAction, frozenset[SupplierRole]]] = {
        SupplierAction.REGISTER: frozenset(
            {
                SupplierRole.SYSTEM,
                SupplierRole.BOSS,
                SupplierRole.PRODUCT,
                SupplierRole.SOURCING,
            }
        ),
        SupplierAction.PRICE_RECORD: frozenset(
            {
                SupplierRole.SYSTEM,
                SupplierRole.BOSS,
                SupplierRole.PRODUCT,
                SupplierRole.SOURCING,
            }
        ),
        SupplierAction.CAPABILITY_SEARCH: frozenset(
            {
                SupplierRole.SYSTEM,
                SupplierRole.BOSS,
                SupplierRole.PRODUCT,
                SupplierRole.SOURCING,
            }
        ),
        SupplierAction.READ: frozenset(
            {
                SupplierRole.SYSTEM,
                SupplierRole.BOSS,
                SupplierRole.PRODUCT,
                SupplierRole.SOURCING,
                SupplierRole.FINANCE,
            }
        ),
    }

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(
        self, actor: SupplierActor, action: SupplierAction, tenant_id: TenantId
    ) -> str:
        if (
            not isinstance(actor, SupplierActor)
            or not isinstance(action, SupplierAction)
            or tenant_id != self._tenant_id
            or actor.tenant_id != tenant_id
            or actor.role not in self._ALLOWED.get(action, frozenset())
        ):
            raise PermissionDenied("Phase 2 供应商授权拒绝")
        return f"phase2:{actor.role.value}:{action.value}"


@runtime_checkable
class SupplierService(Protocol):
    async def register(
        self, tenant_id: TenantId, supplier: Supplier, *, actor: SupplierActor
    ) -> SupplierId: ...

    async def record_price(
        self, tenant_id: TenantId, record: SupplierPriceRecord, *, actor: SupplierActor
    ) -> None:
        """记录价格。

        basis 为 quoted 时必须有报价证据引用，否则拒绝——
        没有证据的 quoted 等于把参考价洗白成可承诺价（硬边界 7）。
        价格记录只增不改：价格历史本身是谈判情报。
        """
        ...

    async def search_by_capability(
        self, tenant_id: TenantId, capability_tags: list[str], *, actor: SupplierActor
    ) -> list[Supplier]:
        """匹配梯子第 4–5 级的查询：现有供应商里找能供或能定制的。"""
        ...

    async def get(
        self, tenant_id: TenantId, supplier_id: SupplierId, *, actor: SupplierActor
    ) -> Supplier: ...


__all__ = (
    "Phase2SupplierAuthorizer",
    "Supplier",
    "SupplierAction",
    "SupplierActor",
    "SupplierAuthorizer",
    "SupplierPriceRecord",
    "SupplierRole",
    "SupplierService",
)
