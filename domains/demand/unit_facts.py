"""单位绑定与完整需求哈希：纯函数，禁止IO与数量类型修正。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from domains.demand.errors import NeedUnitError
from domains.demand.schemas import NeedQuoteFacts
from shared.schemas.identifiers import TenantId, ValidatedNeedId
from shared.schemas.provenance import FactualField


def canonical_value(value: object) -> object:
    """稳定JSON表示含None的完整事实，不强转数量。"""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise NeedUnitError("invalid_input")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return {
            name: canonical_value(getattr(value, name))
            for name in type(value).model_fields
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: canonical_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {key: canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    return value


def canonical_hash(value: object) -> str:
    """UTF-8 canonical JSON SHA256，版本由调用者明确传入。"""
    return hashlib.sha256(
        json.dumps(
            canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def quantity_fact_hash(
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
    quantity: FactualField[int],
) -> str:
    """完整数量来源绑定，历史0仍可读。"""
    if type(quantity.value) is not int:
        raise NeedUnitError("quantity_invalid")
    return canonical_hash(
        {
            "version": "need-quantity-fact-v1",
            "tenant_id": tenant_id,
            "need_id": need_id,
            "quantity": quantity,
        }
    )


def need_quote_facts_hash(facts: NeedQuoteFacts) -> str:
    """全部事实与绑定纳入稳定hash，不包含读取时间。"""
    if facts.quantity is not None:
        quantity_fact_hash(facts.tenant_id, facts.need_id, facts.quantity)
    return canonical_hash({"version": "need-quote-facts-v1", "facts": facts})


def require_current_unit(facts: NeedQuoteFacts) -> FactualField[str]:
    """要求已确认正数量与仍匹配其完整来源的人工单位。"""
    quantity = facts.quantity
    if quantity is None or type(quantity.value) is not int or quantity.value <= 0:
        raise NeedUnitError("quantity_invalid")
    if not quantity.provenance.is_human_confirmed:
        raise NeedUnitError("fact_unconfirmed")
    if facts.unit is None or facts.unit_confirmation_id is None:
        raise NeedUnitError("unit_missing")
    if not facts.unit.provenance.is_human_confirmed:
        raise NeedUnitError("fact_unconfirmed")
    if facts.unit_quantity_fact_hash != quantity_fact_hash(
        facts.tenant_id, facts.need_id, quantity
    ):
        raise NeedUnitError("unit_stale")
    return facts.unit
