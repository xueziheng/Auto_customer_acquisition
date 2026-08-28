"""完整报价创建意图：纯形状和独立版本编码，不作价格/审批业务判断。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    QuoteId,
    TenantId,
)
from shared.schemas.money import _VALID_ROUNDING, Money
from shared.schemas.quote_facts import FactHash, fact_identity, fact_text, fact_utc


def quote_term_text(value: str) -> str:
    """客户条款仅允许换行，不去重、不改写原文本。"""
    fact_text(value.replace("\n", " "))
    return value


QuoteText = Annotated[
    str, Field(min_length=1, max_length=4096), AfterValidator(fact_text)
]
QuoteKey = Annotated[
    str, Field(min_length=1, max_length=128), AfterValidator(fact_text)
]
QuoteTime = Annotated[datetime, AfterValidator(fact_utc)]


class QuoteDTO(BaseModel):
    """严格不可变新契约，未提供字段不补默认值。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _identities(self) -> Self:
        """ID与历史起草人都按身份形状校验，不能藏控制字符。"""
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is not None and (
                name.endswith("_id") or name in {"prepared_by", "confirmed_by"}
            ):
                fact_identity(value)
        return self


class QuoteTerm(QuoteDTO):
    """支持的条款类型；类型与文本的商业一致性由报价域核验。"""

    kind: Literal[
        "discount", "delivery_commitment", "payment_terms", "certification_commitment"
    ]
    text: Annotated[
        str, Field(min_length=1, max_length=4096), AfterValidator(quote_term_text)
    ]


class QuoteRoundingInput(QuoteDTO):
    """显式舍入输入，不设货币精度默认值。"""

    unit_places: Annotated[int, Field(ge=0, le=12)]
    total_places: Annotated[int, Field(ge=0, le=12)]
    strategy: str

    @model_validator(mode="after")
    def _rounding(self) -> Self:
        """复用Money支持集合，不引入第二套舍入策略。"""
        if self.strategy not in _VALID_ROUNDING:
            raise ValueError("未知的舍入策略")
        return self


class QuoteCreationIntent(QuoteDTO):
    """完整创建/修订意图，外部幂等键不是业务意图的一部分。"""

    tenant_id: TenantId
    prepared_by: EmployeeId
    opportunity_id: OpportunityId
    cost_sheet_id: CostSheetId
    expected_context_hash: FactHash
    expected_sheet_hash: FactHash
    valid_until: QuoteTime
    unit_price: Money
    rounding: QuoteRoundingInput
    quote_fx_ref: QuoteText | None
    terms: tuple[QuoteTerm, ...]
    replaces_quote_id: QuoteId | None
    expected_quote_version: Annotated[int, Field(gt=0)] | None
    scope_confirmation_id: str
    scope_confirmation_hash: FactHash

    @model_validator(mode="after")
    def _shape(self) -> Self:
        """修订两字段必须同时出现，实际客户单价必须正值。"""
        if (self.replaces_quote_id is None) != (self.expected_quote_version is None):
            raise ValueError("修订必须同时提供报价ID与预期版本")
        if not self.unit_price.amount.is_finite() or self.unit_price.amount <= 0:
            raise ValueError("客户单价必须为正有限Decimal")
        return self


class QuoteCreationCompletion(QuoteDTO):
    """可信报价持久层提供的完成事实，不接受HTTP自证。"""

    tenant_id: TenantId
    operation_id: str
    request_hash: FactHash
    basis_id: str
    quote_id: QuoteId
    quote_version: Annotated[int, Field(gt=0)]
    quote_content_hash: FactHash
    replaces_quote_id: QuoteId | None
    replaced_quote_version: Annotated[int, Field(gt=0)] | None

    @model_validator(mode="after")
    def _revision_pair(self) -> Self:
        """完成事实保留是否修订及精确前序版本。"""
        if (self.replaces_quote_id is None) != (self.replaced_quote_version is None):
            raise ValueError("完成事实的前序版本必须成对")
        return self


class QuoteCreationOperationView(QuoteDTO):
    """只增创建绑定及唯一首次完成结果。"""

    tenant_id: TenantId
    operation_id: str
    idempotency_key: QuoteKey
    request_hash: FactHash
    intent: QuoteCreationIntent
    basis_id: str
    state: Literal["frozen", "completed"]
    created_at: QuoteTime
    completion: QuoteCreationCompletion | None
    completed_at: QuoteTime | None

    @model_validator(mode="after")
    def _state_shape(self) -> Self:
        """完成时间/事实必须同有；完成记录不能指向另一绑定。"""
        if self.tenant_id != self.intent.tenant_id:
            raise ValueError("操作租户与意图不一致")
        if self.state == "frozen":
            if self.completion is not None or self.completed_at is not None:
                raise ValueError("未完成操作不能带完成事实")
        elif self.completion is None or self.completed_at is None:
            raise ValueError("完成操作缺少完成事实")
        elif (
            self.completion.tenant_id,
            self.completion.operation_id,
            self.completion.request_hash,
            self.completion.basis_id,
        ) != (self.tenant_id, self.operation_id, self.request_hash, self.basis_id):
            raise ValueError("完成事实与操作绑定不一致")
        return self


def require_creation_decimal_resources(value: Decimal) -> None:
    """定点展开前限制系数、指数和编码长度；4096是资源上限，不是业务精度。"""
    if not value.is_finite():
        raise ValueError("金额必须为有限Decimal")
    sign, digits, exponent = value.as_tuple()
    if len(digits) > 4096 or abs(exponent) > 4096:
        raise ValueError("Decimal编码超出资源上限")
    if value.is_zero():
        return
    length = (
        len(digits) + exponent + sign
        if exponent >= 0
        else max(len(digits) + exponent, 1) + 1 - exponent + sign
    )
    if length > 4096:
        raise ValueError("Decimal编码超出资源上限")


def canonical_creation_value(value: object) -> object:
    """不依赖Decimal上下文的无损规范化；与旧事实编码刻意分离。"""
    if isinstance(value, Decimal):
        require_creation_decimal_resources(value)
        if value.is_zero():
            return "0"
        fixed = format(value, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    if isinstance(value, datetime):
        return fact_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return {
            name: canonical_creation_value(getattr(value, name))
            for name in type(value).model_fields
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: canonical_creation_value(getattr(value, f.name))
            for f in fields(value)
        }
    if isinstance(value, dict):
        return {key: canonical_creation_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_creation_value(item) for item in value]
    return value


def canonical_creation_hash(value: object) -> str:
    """版本化载荷的UTF-8 SHA256；版本由业务调用方明确给出。"""
    return hashlib.sha256(
        json.dumps(
            canonical_creation_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def quote_creation_request_hash(intent: QuoteCreationIntent) -> str:
    """完整意图包括None与有序条款，不含操作ID/时钟/幂等键。"""
    return canonical_creation_hash(
        {"version": "quote-create-request-v1", "intent": intent}
    )


def quote_terms_hash(terms: tuple[QuoteTerm, ...]) -> str:
    """有序完整条款用于人工成本适用性绑定。"""
    return canonical_creation_hash({"version": "quote-terms-v1", "terms": terms})
