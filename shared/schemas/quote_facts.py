"""报价内部中立事实契约与T3A原字节编码；不定义单位有效性或权限。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TeamId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import Money
from shared.schemas.provenance import FactualField


def fact_text(value: str) -> str:
    """保留T3A原字符串校验，不静默修正旧事实。"""
    if (
        value != value.strip()
        or not value
        or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)
    ):
        raise ValueError("字符串必须非空且不含首尾空白或控制字符")
    return value


def fact_utc(value: datetime) -> datetime:
    """保留T3A aware→UTC规则。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


def fact_identity(value: object) -> None:
    """身份ID边界原样保留，不约束多态来源或提取者标签。"""
    if type(value) is not str or len(value) > 40:
        raise ValueError("标识必须为不超过40字符的字符串")
    fact_text(value)


FactHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
FactId = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(fact_text)]


class NeedFactDTO(BaseModel):
    """T3A公共类型的原严格/不可变元数据校验。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _metadata(self) -> Self:
        """顶层与事实确认人ID同样校验；完整Provenance时间统一UTC。"""
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is not None and (name.endswith("_id") or name == "confirmed_by"):
                fact_identity(value)
            if isinstance(value, FactualField):
                provenance = value.provenance
                if provenance.confirmed_by is not None:
                    fact_identity(provenance.confirmed_by)
                updated = replace(
                    provenance,
                    extracted_at=fact_utc(provenance.extracted_at),
                    confirmed_at=fact_utc(provenance.confirmed_at)
                    if provenance.confirmed_at
                    else None,
                )
                object.__setattr__(self, name, replace(value, provenance=updated))
        return self


class NeedQuoteFacts(NeedFactDTO):
    """完整Need事实投影，不用展示摘要补造Provenance。"""

    tenant_id: TenantId
    need_id: ValidatedNeedId
    account_id: ProspectAccountId
    status: str
    product_category: FactualField[str]
    application: FactualField[str] | None
    material: FactualField[str] | None
    size_spec: FactualField[str] | None
    packaging: FactualField[str] | None
    destination: FactualField[str] | None
    current_supply_issue: FactualField[str] | None
    certification_required: FactualField[str] | None
    unit: FactualField[str] | None
    quantity: FactualField[int] | None
    required_by: FactualField[date] | None
    target_price: FactualField[Money] | None
    unit_quantity_fact_hash: FactHash | None
    unit_confirmation_id: FactId | None


class QuoteEmployeeFact(NeedFactDTO):
    """本次读取的员工事实，不代表任何用途授权。"""

    tenant_id: TenantId
    employee_id: EmployeeId
    role: Literal[
        "boss", "manager", "sales", "sourcing", "product", "finance", "viewer"
    ]
    is_active: bool
    manager_id: EmployeeId | None
    team_id: TeamId | None


class QuoteRuntimeFacts(NeedFactDTO):
    """运行时员工事实与历史商业内容分离。"""

    current_actor: QuoteEmployeeFact
    owner: QuoteEmployeeFact
    preparer: QuoteEmployeeFact | None


def canonical_fact_value(value: object) -> object:
    """T3A稳定表示：保留Decimal尾零、None、完整来源及原始列表次序。"""
    if isinstance(value, datetime):
        return fact_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return {
            name: canonical_fact_value(getattr(value, name))
            for name in type(value).model_fields
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: canonical_fact_value(getattr(value, f.name)) for f in fields(value)
        }
    if isinstance(value, dict):
        return {key: canonical_fact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_fact_value(item) for item in value]
    return value


def canonical_fact_hash(value: object) -> str:
    """纯编码，错误由调用域映射，版本必须由调用者提供。"""
    return hashlib.sha256(
        json.dumps(
            canonical_fact_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
