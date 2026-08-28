"""报价内部HTTP白名单；不是客户文件许可或可反向提交的可信事实。"""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from domains.quotations.basis_schemas import QuoteCalculationSnapshot
from domains.quotations.context import QuoteSpecificationFacts
from domains.quotations.models import QuoteState
from domains.quotations.version_schemas import QuoteContentLine
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    QuoteId,
    ValidatedNeedId,
)
from shared.schemas.money import Money
from shared.schemas.provenance import ProvenanceSummary
from shared.schemas.quote_creation import QuoteDTO, QuoteTerm, QuoteText, QuoteTime
from shared.schemas.quote_facts import (
    FactHash,
    FactId,
    NeedQuantityPreparationStatus,
    NeedUnitPreparationStatus,
)


class QuoteEmptyCommand(QuoteDTO):
    """需要显式空body的操作；额外控制字段一律拒绝。"""


class QuoteIssuerPublicView(QuoteDTO):
    """老板直接确认的抬头，不带原件展开许可。"""

    issuer_id: str
    content_hash: FactHash
    name: QuoteText
    address: QuoteText
    contact: QuoteText
    source_ref: QuoteText
    confirmed_by: EmployeeId
    confirmed_at: QuoteTime
    field_provenance: dict[str, ProvenanceSummary]


class QuoteNeedPublicSummary(QuoteDTO):
    """仅保留需求值与安全来源摘要，禁止完整FactualField外泄。"""

    need_id: ValidatedNeedId
    account_id: ProspectAccountId
    status: str
    product_category: str
    application: str | None
    material: str | None
    size_spec: str | None
    packaging: str | None
    destination: str | None
    current_supply_issue: str | None
    certification_required: str | None
    unit: str | None
    quantity: int | None
    required_by: date | None
    target_price: Money | None
    unit_quantity_fact_hash: FactHash | None
    unit_confirmation_id: FactId | None
    origins: dict[str, ProvenanceSummary]


class QuotePreparationBlocker(QuoteDTO):
    """用途专属固定字段/原因配对，不接任意异常消息。"""

    field: Literal["quantity", "unit", "destination", "specification", "issuer"]
    code: Literal[
        "facts_missing",
        "quantity_invalid",
        "fact_unconfirmed",
        "unit_missing",
        "unit_stale",
        "issuer_missing",
    ]

    @model_validator(mode="after")
    def _pair(self) -> Self:
        allowed = {
            "quantity": {"facts_missing", "quantity_invalid", "fact_unconfirmed"},
            "unit": {"unit_missing", "unit_stale", "fact_unconfirmed"},
            "destination": {"facts_missing"},
            "specification": {"facts_missing"},
            "issuer": {"issuer_missing"},
        }
        if self.code not in allowed[self.field]:
            raise ValueError("准备缺项字段与原因不匹配")
        return self


class QuotePreparationPublicView(QuoteDTO):
    """缺项仍可显示真实规格/hash；完整业务hash仅在全部事实可用时提供。"""

    opportunity_id: OpportunityId
    account_id: ProspectAccountId
    owner_id: EmployeeId
    prepared_by: EmployeeId
    account_name: str
    country: str
    need: QuoteNeedPublicSummary
    need_facts_hash: FactHash
    specification_hash: FactHash
    specification: QuoteSpecificationFacts
    issuer: QuoteIssuerPublicView | None
    quantity_fact_hash: FactHash | None
    quantity_status: NeedQuantityPreparationStatus
    unit_status: NeedUnitPreparationStatus
    context_hash: FactHash | None
    blockers: tuple[QuotePreparationBlocker, ...]
    checked_at: QuoteTime


class QuoteInternalPublicView(QuoteDTO):
    """已授权内部版本摘要，不包含完整basis/intent/Need/runtime。"""

    quote_id: QuoteId
    opportunity_id: OpportunityId
    version: Annotated[int, Field(gt=0)]
    state: QuoteState
    created_at: QuoteTime
    valid_until: QuoteTime
    prepared_by: EmployeeId
    owner_id: EmployeeId
    replaces_quote_id: QuoteId | None
    replaced_quote_version: Annotated[int, Field(gt=0)] | None
    content_hash: FactHash
    request_hash: FactHash
    issuer: QuoteIssuerPublicView
    account_name: QuoteText
    country: QuoteText
    lines: tuple[QuoteContentLine, ...]
    terms: tuple[QuoteTerm, ...]
    calculation: QuoteCalculationSnapshot
    basis_id: str
    basis_hash: FactHash
    cost_sheet_id: CostSheetId
    scope_confirmation_id: str
