"""报价域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    QuoteId,
    TenantId,
)
from shared.schemas.money import Money


class ForbiddenAutoCommitment(str, Enum):
    """禁止自动承诺清单 —— 本域的核心资产。

    即使 Campaign 已批准，下列内容**永远不能由 Agent 自动发出**。
    ``tool_gateway`` 和 ``agent_runtime/guardrails`` 都对着这个枚举检查。

    每个值的注释说明「自动承诺会发生什么」——理解后果才不会在
    实现时想当然地放宽。
    """

    FIRST_CONCRETE_PRICE = "first_concrete_price"
    """首次具体价格。客户会拿着截图要求兑现；价格一旦说出就成了
    谈判锚点，说错了整单利润就没了。"""

    FORMAL_QUOTATION = "formal_quotation"
    """正式报价。法律意义上的要约。"""

    DISCOUNT = "discount"
    """折扣。自动折扣会被反复试探（"再便宜点"），底线一次次后退。"""

    STOCK_COMMITMENT = "stock_commitment"
    """库存承诺。库存数据从来不完全准确，承诺了没货是重大违约。"""

    DELIVERY_DATE_COMMITMENT = "delivery_date_commitment"
    """交货期承诺。交期依赖供应商和物流，Agent 无法核实。"""

    CERTIFICATION_COMMITMENT = "certification_commitment"
    """认证承诺。"有 CE 认证"说错了会导致清关失败和法律责任。"""

    PAYMENT_TERMS = "payment_terms"
    """付款条件。直接决定现金流风险。"""

    CONTRACT_TERMS = "contract_terms"
    """合同条款。"""

    EXCLUSIVE_DISTRIBUTION = "exclusive_distribution"
    """独家代理。锁死一个市场的决定不能由模型做。"""

    QUALITY_GUARANTEE = "quality_guarantee"
    """质量保证。"保证不生锈"这种话是售后成本的来源。"""

    OFF_CATALOG_REFERENCE_PRICE = "off_catalog_reference_price"
    """目录外产品的客户参考价。此时手里只有 INDICATIVE 价格，
    给出参考价等于用猜测做承诺（硬边界 7 的邮件侧）。"""


class QuoteState(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


ALLOWED_TRANSITIONS: dict[QuoteState, set[QuoteState]] = {
    QuoteState.DRAFT: {QuoteState.PENDING_APPROVAL},
    QuoteState.PENDING_APPROVAL: {QuoteState.APPROVED, QuoteState.REJECTED},
    QuoteState.APPROVED: {QuoteState.SENT, QuoteState.SUPERSEDED},
    QuoteState.SENT: {
        QuoteState.ACCEPTED,
        QuoteState.REJECTED,
        QuoteState.EXPIRED,
        QuoteState.SUPERSEDED,
    },
    QuoteState.ACCEPTED: set(),
    QuoteState.REJECTED: set(),
    QuoteState.EXPIRED: {QuoteState.SUPERSEDED},
    QuoteState.SUPERSEDED: set(),
}
"""注意 ``DRAFT`` 不能直接到 ``SENT``：审批不可跳过。
这张表就是「不允许跳过审批」的机器可执行形式。"""


@dataclass(frozen=True)
class QuoteLine:
    """报价行。

    字段：
        line_number
        description:      客户可见描述
        quantity
        unit_price:       单价（Money）
        line_total
        price_snapshot_ref: 价格快照引用。**必须是 QUOTED 基准**，
                          创建时校验（硬边界 7 的第二道门）
        moq, lead_time_days
    """

    line_number: int
    description: str
    quantity: int
    unit_price: Money
    line_total: Money
    price_snapshot_ref: str
    moq: int | None = None
    lead_time_days: int | None = None


@dataclass
class Quote:
    """报价。**版本不可变**：修改 = 创建新版本，旧版本转 SUPERSEDED。

    字段：
        quote_id, tenant_id, opportunity_id
        version
        state
        lines
        currency
        total:            总额
        valid_until:      有效期，**必填**——没有有效期的报价是
                          开放式承诺，三个月后客户拿旧价下单时
                          汇率和供应商价格早变了
        cost_sheet_id:    依据的成本表（QUOTED 版本，已锁定）
        fx_snapshot_id
        payment_terms_note: 付款条件说明（人工填写，属禁止自动承诺项）
        prepared_by, approved_by, approved_at
        sent_at, decided_at
        customer_feedback: 客户对报价的反馈原文
        created_at
    """

    quote_id: QuoteId
    tenant_id: TenantId
    opportunity_id: OpportunityId
    version: int
    currency: str
    total: Money
    valid_until: datetime
    cost_sheet_id: CostSheetId
    created_at: datetime
    lines: list[QuoteLine] = field(default_factory=list)
    state: QuoteState = QuoteState.DRAFT
    fx_snapshot_id: FxSnapshotId | None = None
    payment_terms_note: str | None = None
    prepared_by: EmployeeId | None = None
    approved_by: EmployeeId | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    decided_at: datetime | None = None
    customer_feedback: str | None = None

    def can_transition_to(self, target: QuoteState) -> bool:
        return target in ALLOWED_TRANSITIONS[self.state]

    def is_expired_at(self, now: datetime) -> bool:
        """是否已过有效期。``scheduler_worker`` 定期扫描调用，
        过期自动转 EXPIRED 并通知负责人。"""
        return now >= self.valid_until
