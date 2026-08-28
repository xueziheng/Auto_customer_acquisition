"""新版本报价DTO；旧dataclass构造保持原样。"""

from decimal import Decimal, Context, localcontext
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from domains.quotations.basis_schemas import BasisDTO, Hash, Positive, QuoteBasis
from domains.quotations.context import QuoteIssuer
from domains.quotations.errors import QuotationError
from domains.quotations.models import QuoteState
from shared.schemas.identifiers import CostSheetId, EmployeeId, MessageAttemptId, OpportunityId, QuoteId, TenantId
from shared.schemas.money import Money
from shared.schemas.quote_creation import QuoteCreationIntent, QuoteRoundingInput, QuoteTerm, QuoteText, QuoteTime, require_creation_decimal_resources


class QuotationActor(BasisDTO):
    """可信上层构造的身份声明，服务仍须重读当前事实。"""
    employee_id: EmployeeId
    role: Literal["boss", "manager", "sales", "sourcing", "product", "finance", "viewer"]


class QuoteDraftCommand(BasisDTO):
    """外部创建输入不接收任何确认、锁定或审批自证字段。"""
    opportunity_id: OpportunityId
    cost_sheet_id: CostSheetId
    expected_context_hash: Hash
    expected_sheet_hash: Hash
    scope_confirmation_id: str
    unit_price: Money
    rounding: QuoteRoundingInput
    quote_fx_ref: QuoteText | None
    valid_until: QuoteTime
    terms: tuple[QuoteTerm, ...]
    replaces_quote_id: QuoteId | None
    expected_quote_version: Positive | None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        """人工价沿用Numeric(28,12)上限，不把共享资源限制当业务精度。"""
        if (self.replaces_quote_id is None) != (self.expected_quote_version is None):
            raise ValueError("修订身份必须成对")
        price = self.unit_price.amount
        try:
            require_creation_decimal_resources(price)
            with localcontext(Context(prec=50)):
                if price <= 0 or price >= Decimal("1E16") or price != price.quantize(Decimal("1E-12")):
                    raise ValueError("人工售价精度无效")
        except (ValueError, ArithmeticError):
            raise QuotationError("invalid_input") from None
        return self


class QuoteIssuerCreate(BasisDTO):
    """老板手工确认的原始三字段，不设公司样例默认值。"""
    name: QuoteText
    address: QuoteText
    contact: QuoteText


class QuoteContentLine(BasisDTO):
    """单产品展示价×数量按显式规则舍入，不沿用旧行的未舍入约束。"""
    line_number: Annotated[int, Field(ge=1, le=1)]
    description: QuoteText
    specification: str
    unit: QuoteText
    quantity: Positive
    unit_price: Money
    line_total: Money
    rounding: QuoteRoundingInput

    @model_validator(mode="after")
    def _total(self) -> Self:
        """只验行展示算术，不重算利润；显式高精度不受调用方Decimal上下文影响。"""
        if not self.specification or self.unit_price.amount <= 0:
            raise ValueError("报价行无效")
        with localcontext(Context(prec=80)):
            expected = self.unit_price.multiply(self.quantity).round_to(self.rounding.total_places, self.rounding.strategy)
        if expected != self.line_total:
            raise ValueError("报价行展示总额不一致")
        return self


class QuoteContentSnapshot(BasisDTO):
    """持久版本内容；嵌套字典逐次hash校验，不提供修改后保存入口。"""
    tenant_id: TenantId
    quote_id: QuoteId
    opportunity_id: OpportunityId
    version: Positive
    operation_id: str
    request_hash: Hash
    intent: QuoteCreationIntent
    basis: QuoteBasis
    prepared_by: EmployeeId
    owner_id: EmployeeId
    issuer: QuoteIssuer
    account_name: QuoteText
    country: QuoteText
    lines: Annotated[tuple[QuoteContentLine, ...], Field(min_length=1, max_length=1)]
    terms: tuple[QuoteTerm, ...]
    valid_until: QuoteTime
    replaces_quote_id: QuoteId | None
    replaced_quote_version: Positive | None
    created_at: QuoteTime
    content_hash: Hash


class QuoteDetailView(BasisDTO):
    """内部读取完整快照，不可直接HTTP序列化。"""
    content: QuoteContentSnapshot
    state: QuoteState


class QuoteSendReceipt(BasisDTO):
    """实际发送事实，不接受下载事件或调用方成功标志。"""
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    quote_id: QuoteId
    content_hash: Hash
    sent_at: QuoteTime


class QuoteStateEvent(BasisDTO):
    """本域状态审计；tenant由绑定仓储提供。"""
    event_id: str
    quote_id: QuoteId
    from_state: QuoteState | None
    to_state: QuoteState
    actor_id: EmployeeId | None
    reason: Literal["created", "revision", "expiry", "verified_send", "approval_submitted", "approval_approved", "approval_rejected"]
    at: QuoteTime
    reference_id: str | None


class StoredQuoteIssuer(BasisDTO):
    """抬头版本与幂等请求绑定，不由时间戳决定当前版本。"""
    issuer: QuoteIssuer
    version: Positive
    idempotency_key: str
    request_hash: Hash
