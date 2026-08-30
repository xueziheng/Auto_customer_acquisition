"""供应商服务的 authorizer-first、tenant-bound 实现。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from domains.suppliers.models import Supplier, SupplierPriceRecord
from domains.suppliers.repository import SuppliersUnitOfWork
from domains.suppliers.service import (
    SupplierAction,
    SupplierActor,
    SupplierAuthorizer,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import SupplierId, TenantId
from shared.schemas.money import PriceBasis

_SPACE_RE = re.compile(r"\s+")


def _normalize(value: str) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold()


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} 必须含时区")


class SupplierServiceImpl:
    """价格只追加，不在本域产生报价或修改历史记录。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], SuppliersUnitOfWork],
        authorizer: SupplierAuthorizer,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValidationError("供应商事务依赖无效")
        if not isinstance(authorizer, SupplierAuthorizer):
            raise ValidationError("供应商授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self, tenant_id: TenantId, actor: SupplierActor, action: SupplierAction
    ) -> None:
        self._authorizer.require(actor, action, tenant_id)

    async def register(
        self, tenant_id: TenantId, supplier: Supplier, *, actor: SupplierActor
    ) -> SupplierId:
        self._require(tenant_id, actor, SupplierAction.REGISTER)
        if not isinstance(supplier, Supplier):
            raise ValidationError("供应商参数无效")
        if supplier.tenant_id != tenant_id:
            raise ValidationError("供应商租户与请求租户不一致")
        if not supplier.name or supplier.name != supplier.name.strip():
            raise ValidationError("供应商名称必须非空且无首尾空白")
        async with self._uow_factory(tenant_id) as uow:
            await uow.suppliers.add(tenant_id, supplier)
        return supplier.supplier_id

    async def record_price(
        self,
        tenant_id: TenantId,
        record: SupplierPriceRecord,
        *,
        actor: SupplierActor,
    ) -> None:
        self._require(tenant_id, actor, SupplierAction.PRICE_RECORD)
        if not isinstance(record, SupplierPriceRecord):
            raise ValidationError("供应商价格参数无效")
        if record.tenant_id != tenant_id:
            raise ValidationError("供应商价格租户与请求租户不一致")
        if (
            not record.product_desc
            or record.product_desc != record.product_desc.strip()
        ):
            raise ValidationError("供应商价格产品描述必须非空且无首尾空白")
        if (
            isinstance(record.quantity_tier, bool)
            or not isinstance(record.quantity_tier, int)
            or record.quantity_tier < 1
        ):
            raise ValidationError("供应商价格数量档必须为正整数")
        amount = record.price.amount
        if (
            not isinstance(amount, Decimal)
            or not amount.is_finite()
            or amount <= Decimal(0)
        ):
            raise ValidationError("供应商价格必须为有限正 Decimal")
        if not str(record.evidence_ref).strip():
            raise ValidationError("供应商价格必须有 Artifact Evidence")
        _aware(record.observed_at, "observed_at")
        now = self._now()
        _aware(now, "供应商服务时钟")
        if record.basis == PriceBasis.QUOTED:
            if record.valid_until is None:
                raise ValidationError("quoted 价格必须有明确有效期")
            _aware(record.valid_until, "valid_until")
            if record.valid_until <= record.observed_at or record.valid_until <= now:
                raise ValidationError("quoted 价格在记录时必须仍然有效")
        elif record.basis == PriceBasis.INDICATIVE:
            if record.valid_until is not None:
                raise ValidationError("indicative 价格不得伪装成带有效期的 quoted")
        else:
            raise ValidationError("供应商价格 basis 只允许 indicative 或 quoted")
        async with self._uow_factory(tenant_id) as uow:
            await uow.prices.add(tenant_id, record)

    async def search_by_capability(
        self,
        tenant_id: TenantId,
        capability_tags: list[str],
        *,
        actor: SupplierActor,
    ) -> list[Supplier]:
        self._require(tenant_id, actor, SupplierAction.CAPABILITY_SEARCH)
        normalized = sorted(
            {_normalize(tag) for tag in capability_tags if _normalize(tag)}
        )
        if not normalized:
            return []
        async with self._uow_factory(tenant_id) as uow:
            rows = await uow.suppliers.search_by_tags(tenant_id, normalized)
        requested = set(normalized)
        matched = [
            supplier
            for supplier in rows
            if supplier.tenant_id == tenant_id
            and not requested.isdisjoint(
                {_normalize(tag) for tag in supplier.capability_tags}
            )
        ]
        return sorted(matched, key=lambda item: str(item.supplier_id))[:50]

    async def get(
        self, tenant_id: TenantId, supplier_id: SupplierId, *, actor: SupplierActor
    ) -> Supplier:
        self._require(tenant_id, actor, SupplierAction.READ)
        async with self._uow_factory(tenant_id) as uow:
            supplier = await uow.suppliers.get(tenant_id, supplier_id)
        if supplier is None:
            raise ValidationError("供应商不存在或租户不匹配")
        return supplier


__all__ = ("SupplierServiceImpl",)
