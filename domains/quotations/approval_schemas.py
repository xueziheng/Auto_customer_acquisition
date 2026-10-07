"""报价单轮审批的严格事实；安全商业载荷与内部完整上下文分离。"""

from typing import Literal

from domains.quotations.basis_schemas import (
    BasisDTO,
    Hash,
    Positive,
    QuoteProfitMetrics,
)
from domains.quotations.context import QuoteBusinessContext
from domains.quotations.errors import QuoteApprovalErrorCode
from domains.quotations.version_schemas import QuoteContentLine, QuoteDetailView
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    OpportunityId,
    QuoteId,
    RunId,
    TenantId,
)
from shared.schemas.money import Money, WireDecimal
from shared.schemas.quote_creation import QuoteTerm, QuoteText, QuoteTime
from shared.schemas.quote_facts import QuoteEmployeeFact

QuoteApprovalType = Literal[
    "quote_send",
    "margin_floor_override",
    "discount",
    "delivery_commitment",
    "payment_terms",
    "certification_commitment",
]


class QuoteApprovalCustomerSummary(BasisDTO):
    """客户将看到的内容，不包含来源与内部人员。"""

    issuer_name: QuoteText
    issuer_address: QuoteText
    issuer_contact: QuoteText
    account_name: QuoteText
    country: QuoteText
    line: QuoteContentLine
    valid_until: QuoteTime
    terms: tuple[QuoteTerm, ...]


class QuoteApprovalFxSummary(BasisDTO):
    """实际汇率与稳定引用；引用不是原件访问授权。"""

    source_currency: QuoteText
    target_currency: QuoteText
    rate: WireDecimal
    observed_at: QuoteTime
    reference_id: str


class QuoteApprovalCalculationSummary(BasisDTO):
    """已完成的确定性计算，只投影不重算。"""

    base_currency: QuoteText
    quote_currency: QuoteText
    effective_unit_revenue: Money
    displayed_unit_price: Money
    displayed_total: Money
    metrics: QuoteProfitMetrics
    cost_fx_rates: tuple[QuoteApprovalFxSummary, ...]
    quote_fx: QuoteApprovalFxSummary | None


class QuoteApprovalPolicySummary(BasisDTO):
    """审批所针对政策的安全身份与阈值。"""

    policy_id: str
    content_hash: Hash
    category: QuoteText | None
    minimum_margin_rate: WireDecimal
    target_margin_rate: WireDecimal
    effective_from: QuoteTime


class QuoteApprovalEvidenceSummary(BasisDTO):
    """明确人工确认事实，不泄露原件路径或原文。"""

    evidence_id: str
    evidence_hash: Hash
    kind: Literal["supplier_price", "confirmed_expense"]
    basis: Literal["quoted", "actual"]
    amount: Money
    valid_until: QuoteTime | None
    confirmed_by: EmployeeId
    confirmed_at: QuoteTime


class QuoteApprovalPreviousSummary(BasisDTO):
    """实际上一版本的商业差异，不只读取replaces。"""

    quote_id: QuoteId
    version: Positive
    content_hash: Hash
    customer: QuoteApprovalCustomerSummary
    calculation: QuoteApprovalCalculationSummary
    policy: QuoteApprovalPolicySummary


class QuoteApprovalPackagePayload(BasisDTO):
    """审批HTTP可展示的完整白名单，不包含原始资料。"""

    schema_version: Literal["quote-approval-v1"]
    tenant_id: TenantId
    quote_id: QuoteId
    quote_version: Positive
    opportunity_id: OpportunityId
    content_hash: Hash
    context_hash: Hash
    basis_id: str
    basis_hash: Hash
    prepared_by: EmployeeId
    submitted_owner_id: EmployeeId
    approval_type: QuoteApprovalType
    required_types: tuple[QuoteApprovalType, ...]
    policy: QuoteApprovalPolicySummary
    customer: QuoteApprovalCustomerSummary
    calculation: QuoteApprovalCalculationSummary
    evidence: tuple[QuoteApprovalEvidenceSummary, ...]
    previous: QuoteApprovalPreviousSummary | None


class QuoteApprovalSnapshot(BasisDTO):
    """仅供受信workflow的完整组包输入。"""

    internal_quote: QuoteDetailView
    required_types: tuple[QuoteApprovalType, ...]
    payloads: tuple[QuoteApprovalPackagePayload, ...]
    expires_at_limit: QuoteTime


class QuoteApprovalDecisionSnapshot(BasisDTO):
    """不随应用记账变化的独立决定快照。"""

    tenant_id: TenantId
    approval_id: ApprovalId
    approval_type: QuoteApprovalType
    change_set_ref: str
    request_hash: Hash
    payload: QuoteApprovalPackagePayload
    created_at: QuoteTime
    expires_at: QuoteTime
    expires_at_limit: QuoteTime
    prepared_by: EmployeeId
    submitted_owner_id: EmployeeId
    proposed_by_run: RunId | None
    decision: Literal["approve", "reject"] | None
    decided_by: EmployeeId | None
    decided_at: QuoteTime | None
    decision_note: QuoteText | None


class QuoteApprovalFact(QuoteApprovalDecisionSnapshot):
    """真实持久审批事实，不从事件或UI权限生成。"""

    state: Literal[
        "pending", "approved", "applied", "apply_failed", "rejected", "expired"
    ]
    applied_at: QuoteTime | None
    application_error_code: QuoteText | None


class QuoteApprovalSubmission(BasisDTO):
    """单轮精确集合与不可变提交身份。"""

    tenant_id: TenantId
    quote_id: QuoteId
    quote_version: Positive
    content_hash: Hash
    policy_id: str
    policy_hash: Hash
    required_types: tuple[QuoteApprovalType, ...]
    facts: tuple[QuoteApprovalFact, ...]


class QuoteApprovalApplicationReceipt(BasisDTO):
    """仅报价事务首次成功产生，应用状态不能反推此事实。"""

    tenant_id: TenantId
    quote_id: QuoteId
    quote_version: Positive
    content_hash: Hash
    facts_hash: Hash
    decisions: tuple[QuoteApprovalDecisionSnapshot, ...]
    applied_at: QuoteTime
    quote_send_decider: EmployeeId
    approval_run_id: RunId


QuoteApprovalOutcome = Literal[
    "waiting",
    "approved",
    "rejected",
    "expired",
    "blocked",
    "obsolete",
    "already_applied",
]


class QuoteApprovalApplyResult(BasisDTO):
    """报价应用结果，固定错误码不泄漏来源或存储异常。"""

    outcome: QuoteApprovalOutcome
    quote: QuoteDetailView
    receipt: QuoteApprovalApplicationReceipt | None
    error_code: QuoteApprovalErrorCode | None


class QuoteApprovalPollResult(BasisDTO):
    """轮询只返回元数据，不把报价正文写入workflow context。"""

    outcome: Literal[
        "waiting",
        "ready",
        "rejected",
        "expired",
        "blocked",
        "obsolete",
        "already_applied",
    ]
    approval_ids: tuple[ApprovalId, ...]
    deadline: QuoteTime | None
    error_code: QuoteApprovalErrorCode | None


class QuoteWorkflowExecutor(BasisDTO):
    """受信handler的技术归属声明，本身不证明run存在。"""

    workflow_type: Literal["quote_approval"]
    run_id: RunId
    quote_id: QuoteId


class QuoteWorkflowRunFact(BasisDTO):
    """真实run元数据，不含context或正文。"""

    tenant_id: TenantId
    run_id: RunId
    workflow_type: str
    workflow_version: int
    subject_ref: str
    quote_version: Positive
    content_hash: Hash


class QuoteApprovalAccessContext(BasisDTO):
    """历史审批读取只需当前员工与机会关联，不依赖Need/issuer。"""

    tenant_id: TenantId
    opportunity_id: OpportunityId
    actor: QuoteEmployeeFact
    owner: QuoteEmployeeFact
    prepared_by: EmployeeId
    submitted_owner_id: EmployeeId


class QuoteApprovalContext(BasisDTO):
    """本轮一次锁齐的所有真实决策人及当前业务事实。"""

    business: QuoteBusinessContext
    deciders: tuple[QuoteEmployeeFact, ...]


class QuoteApprovalAccessResult(BasisDTO):
    """读取许可不自动授予决定权；当前角色供可信身份一致性检查。"""

    can_decide: bool
    current_role: Literal[
        "boss", "manager", "sales", "sourcing", "product", "finance", "viewer"
    ]


class QuoteApprovalSubject(BasisDTO):
    """经不可变绑定核验后的最小审批对象。"""

    tenant_id: TenantId
    approval_id: ApprovalId | None
    quote_id: QuoteId
    quote_version: Positive
    content_hash: Hash
    opportunity_id: OpportunityId
    prepared_by: EmployeeId
    submitted_owner_id: EmployeeId
    approval_type: QuoteApprovalType
