"""产品域的显式角色与动作授权。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Protocol, runtime_checkable

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId


class ProductRole(str, Enum):
    SYSTEM = "system"
    BOSS = "boss"
    PRODUCT = "product"
    SOURCING = "sourcing"
    FINANCE = "finance"
    SALES = "sales"
    CUSTOMER = "customer"


class ProductAction(str, Enum):
    MATCH_SEARCH = "match_search"
    CANDIDATE_CREATE = "candidate_create"
    INTERNAL_VIEW = "internal_view"
    SALES_VIEW = "sales_view"
    CUSTOMER_VIEW = "customer_view"


@dataclass(frozen=True)
class ProductActor:
    """由可信身份服务构造；请求体不得自报 role。"""

    actor_id: str
    role: ProductRole
    tenant_id: TenantId

    def __post_init__(self) -> None:
        if not self.actor_id or self.actor_id != self.actor_id.strip():
            raise ValidationError("产品操作身份无效")
        if not isinstance(self.role, ProductRole):
            raise ValidationError("产品操作角色无效")
        if not str(self.tenant_id).strip():
            raise ValidationError("产品操作租户无效")


@runtime_checkable
class ProductAuthorizer(Protocol):
    def require(
        self, actor: ProductActor, action: ProductAction, tenant_id: TenantId
    ) -> str: ...


class Phase2ProductAuthorizer:
    """仅开放三视图和 V2 内部候选卡所需的最小权限。"""

    _ALLOWED: ClassVar[dict[ProductAction, frozenset[ProductRole]]] = {
        ProductAction.MATCH_SEARCH: frozenset(
            {
                ProductRole.SYSTEM,
                ProductRole.BOSS,
                ProductRole.PRODUCT,
                ProductRole.SOURCING,
            }
        ),
        ProductAction.CANDIDATE_CREATE: frozenset({ProductRole.SYSTEM}),
        ProductAction.INTERNAL_VIEW: frozenset(
            {
                ProductRole.BOSS,
                ProductRole.PRODUCT,
                ProductRole.SOURCING,
                ProductRole.FINANCE,
            }
        ),
        ProductAction.SALES_VIEW: frozenset(
            {
                ProductRole.BOSS,
                ProductRole.PRODUCT,
                ProductRole.SOURCING,
                ProductRole.SALES,
            }
        ),
        ProductAction.CUSTOMER_VIEW: frozenset(
            {
                ProductRole.BOSS,
                ProductRole.PRODUCT,
                ProductRole.SOURCING,
                ProductRole.SALES,
                ProductRole.CUSTOMER,
            }
        ),
    }

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(
        self, actor: ProductActor, action: ProductAction, tenant_id: TenantId
    ) -> str:
        if (
            not isinstance(actor, ProductActor)
            or not isinstance(action, ProductAction)
            or tenant_id != self._tenant_id
            or actor.tenant_id != tenant_id
            or actor.role not in self._ALLOWED.get(action, frozenset())
        ):
            raise PermissionDenied("Phase 2 产品授权拒绝")
        return f"phase2:{actor.role.value}:{action.value}"


__all__ = (
    "Phase2ProductAuthorizer",
    "ProductAction",
    "ProductActor",
    "ProductAuthorizer",
    "ProductRole",
)
