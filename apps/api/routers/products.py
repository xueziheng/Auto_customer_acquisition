"""Product & Supply Center 的安全视图；列表不含供应商、成本或客户报价。"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from domains.products.permissions import ProductActor, ProductRole
from domains.products.schemas import ProductSupplyCardView
from domains.products.service import (
    ProductCustomerView,
    ProductInternalView,
    ProductSalesView,
    ProductService,
)
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import ProductId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_PRODUCT_ID = re.compile(r"prd_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_INTERNAL_ROLES = frozenset({"boss", "product", "sourcing", "finance"})
_SALES_ROLES = frozenset({"boss", "product", "sourcing", "sales"})
_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorResponse},
    403: {"model": ApiErrorResponse},
    404: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}


def _product_id(value: str) -> ProductId:
    if _PRODUCT_ID.fullmatch(value) is None:
        raise ValidationError("产品标识无效")
    return ProductId(value)


def _actor(identity: RequestIdentity, *, allowed_roles: frozenset[str]) -> ProductActor:
    """产品 actor 严格从已解析员工身份派生，body/query/header 无权表达角色。"""

    if identity.employee.role not in allowed_roles:
        raise PermissionDenied("API role gate 默认拒绝")
    return ProductActor(
        actor_id=str(identity.employee.employee_id),
        role=ProductRole(identity.employee.role),
        tenant_id=identity.tenant_id,
    )


def _products(dependencies: ConfiguredApiDependencies) -> ProductService:
    if dependencies.products is None:
        raise TransientError("产品服务尚未配置")
    return dependencies.products


@router.get("", response_model=list[ProductSupplyCardView], responses=_ERRORS)
async def list_supply_cards(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    source_only: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> list[ProductSupplyCardView]:
    return list(
        await _products(dependencies).list_supply_cards(
            identity.tenant_id,
            actor=_actor(identity, allowed_roles=_INTERNAL_ROLES),
            source_only=source_only,
            limit=limit,
        )
    )


@router.get(
    "/{product_id}/internal", response_model=ProductInternalView, responses=_ERRORS
)
async def get_internal_view(
    product_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ProductInternalView:
    return await _products(dependencies).get_internal_view(
        identity.tenant_id,
        _product_id(product_id),
        actor=_actor(identity, allowed_roles=_INTERNAL_ROLES),
    )


@router.get("/{product_id}/sales", response_model=ProductSalesView, responses=_ERRORS)
async def get_sales_view(
    product_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ProductSalesView:
    return await _products(dependencies).get_sales_view(
        identity.tenant_id,
        _product_id(product_id),
        actor=_actor(identity, allowed_roles=_SALES_ROLES),
    )


@router.get(
    "/{product_id}/customer", response_model=ProductCustomerView, responses=_ERRORS
)
async def get_customer_view(
    product_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ProductCustomerView:
    return await _products(dependencies).get_customer_view(
        identity.tenant_id,
        _product_id(product_id),
        actor=_actor(identity, allowed_roles=_SALES_ROLES),
    )


__all__ = ("router",)
