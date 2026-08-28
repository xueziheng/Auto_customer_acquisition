"""文件当前用途事实及无成本版本发现；快照不是跨请求授权票据。"""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from domains.quotations.context import QuoteBusinessContext
from domains.quotations.file_schemas import Hash, Positive, QuoteFileView, Timestamp
from domains.quotations.models import QuoteState
from shared.schemas.identifiers import OpportunityId, QuoteId, RunId, TenantId
from shared.schemas.quote_creation import QuoteDTO
from shared.schemas.quote_document import CustomerQuoteView
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_identity

QuoteFileAction = Literal["generate", "download_current", "read_history"]
QuoteFileBlockerCode = Literal[
    "quote_inactive",
    "quote_expired",
    "approval_missing",
    "approval_invalid",
    "approval_expired",
    "decider_invalid",
    "context_changed",
    "policy_stale",
    "basis_invalid",
]
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"


class QuoteFileScopeFacts(QuoteDTO):
    """仅当前员工/机会范围，不含Need或原起草人许可。"""

    tenant_id: TenantId = Field(pattern=rf"^tn_{_ULID}$")
    opportunity_id: Annotated[OpportunityId, AfterValidator(fact_identity)]
    actor: QuoteEmployeeFact
    owner: QuoteEmployeeFact


class QuoteFileCurrentFacts(QuoteDTO):
    """锁齐员工及Need的当前事实，actor独立于所有审批决定人。"""

    business: QuoteBusinessContext
    deciders: tuple[QuoteEmployeeFact, ...]


class QuoteFormalFileSnapshot(QuoteDTO):
    """仅检查时点的正式文件投影，不对外暴露审批/需求事实。"""

    tenant_id: TenantId = Field(pattern=rf"^tn_{_ULID}$")
    quote_id: QuoteId = Field(pattern=rf"^quo_{_ULID}$")
    opportunity_id: OpportunityId
    quote_version: Positive
    quote_content_hash: Hash
    customer_content_hash: Hash
    approval_run_id: RunId = Field(pattern=rf"^run_{_ULID}$")
    approval_facts_hash: Hash
    template_version: str
    customer: CustomerQuoteView = Field(repr=False)
    checked_at: Timestamp


class QuoteFileActionBlocker(QuoteDTO):
    """确定性商业阻断；基础设施故障不能降格为此DTO。"""

    action: QuoteFileAction
    code: QuoteFileBlockerCode


class QuoteCustomerFileEntry(QuoteDTO):
    """仅安全关联及当时允许的动作提示。"""

    file: QuoteFileView
    allowed_actions: tuple[QuoteFileAction, ...]


class QuoteCustomerVersionView(QuoteDTO):
    """无金额、成本、Need、Provenance和审批载荷。"""

    quote_id: QuoteId = Field(pattern=rf"^quo_{_ULID}$")
    version: Positive
    state: QuoteState
    created_at: Timestamp
    valid_until: Timestamp
    is_past_valid_until: bool
    files: tuple[QuoteCustomerFileEntry, ...]
    allowed_actions: tuple[QuoteFileAction, ...]
    blockers: tuple[QuoteFileActionBlocker, ...]
    checked_at: Timestamp


class QuoteCustomerVersionPage(QuoteDTO):
    """降序游标仅在实际还有下一页时提供。"""

    items: tuple[QuoteCustomerVersionView, ...]
    next_before_version: Positive | None
