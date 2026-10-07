"""报价域服务 —— **本域的公共 API**。"""

from __future__ import annotations

import re
import unicodedata
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol, runtime_checkable

from domains.quotations.approval_display import project_quote_approval_display
from domains.quotations.approval_rules import (
    parse_quote_approval_payload,
    quote_approval_facts_hash,
    quote_approval_payload_hash,
    quote_approval_payloads,
    quote_change_set_ref,
    require_quote_approval_access,
    required_quote_approvals,
)
from domains.quotations.approval_schemas import (
    QuoteApprovalAccessResult,
    QuoteApprovalApplicationReceipt,
    QuoteApprovalSnapshot,
    QuoteApprovalSubject,
    QuoteWorkflowExecutor,
)
from domains.quotations.approval_service import (
    QuoteApprovalPolicyReader,
    QuoteApprovalSession,
    QuotePolicySelection,
    QuoteWorkflowRunReader,
)
from domains.quotations.context import (
    QuoteContextProvider,
    QuoteIssuerReader,
    canonical_quote_specification,
    quote_context_hash,
    quote_specification,
    quote_specification_hash,
)
from domains.quotations.context import (
    QuotePreparationFacts as QuotePreparationFacts,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.customer_versions import (
    QuoteCustomerVersionsService,
    QuoteCustomerVersionsServiceImpl,
)
from domains.quotations.errors import (
    QuoteContextError as QuoteContextError,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.errors import (
    QuoteContextUnavailableError as QuoteContextUnavailableError,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.file_access import (
    ContextQuoteFileScopeAuthorizer,
    QuoteFileAccessService,
    QuoteFileAccessServiceImpl,
    QuoteFileApprovalFactsReader,
    QuoteFileNeedValidator,
    require_quote_file_scope,
)
from domains.quotations.file_service import (
    QuoteFileScopeAuthorizer,
    QuoteFileService,
    QuoteGeneratedArtifactReader,
)
from domains.quotations.http_projection import (
    project_internal_quote as project_internal_quote,  # noqa: PLC0414 - 公共纯投影
)
from domains.quotations.http_projection import (
    project_issuer as project_issuer,  # noqa: PLC0414 - 公共纯投影
)
from domains.quotations.http_projection import (
    project_need as project_need,  # noqa: PLC0414 - 公共纯投影
)
from domains.quotations.http_schemas import (
    QuoteEmptyCommand as QuoteEmptyCommand,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.http_schemas import (
    QuoteInternalPublicView as QuoteInternalPublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.http_schemas import (
    QuoteIssuerPublicView as QuoteIssuerPublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.http_schemas import (
    QuoteNeedPublicSummary as QuoteNeedPublicSummary,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.http_schemas import (
    QuotePreparationPublicView as QuotePreparationPublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.models import ForbiddenAutoCommitment
from domains.quotations.permissions import (
    QuotePreparationPolicy,
    StrictQuotePreparationPolicy,
)
from domains.quotations.preparation_read import (
    QuoteNeedPreparationProjector as QuoteNeedPreparationProjector,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.preparation_read import (
    QuotePreparationReadService as QuotePreparationReadService,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.preparation_read import (
    QuotePreparationReadServiceImpl as QuotePreparationReadServiceImpl,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.quotations.schemas import (
    QuotationActor,
    QuoteBasis,
    QuoteBusinessContext,
    QuoteCreateRequest,
    QuoteCustomerFileEntry,
    QuoteCustomerVersionPage,
    QuoteCustomerVersionView,
    QuoteDetailView,
    QuoteFileApprovalFact,
    QuoteFileBlockerCode,
    QuoteFileCurrentFacts,
    QuoteFileScopeFacts,
    QuoteFileView,
    QuoteFormalFileSnapshot,
    QuoteGeneratedArtifactFact,
    QuoteIssuer,
    QuoteIssuerCreate,
    QuoteSendReceipt,
    QuoteView,
)
from domains.quotations.version_repository import (
    QuotationUowFactory,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    MessageAttemptId,
    OpportunityId,
    QuoteId,
    TenantId,
)
from shared.schemas.quote_creation import QuoteCreationCompletion, QuoteCreationIntent
from shared.schemas.quote_document import (
    CustomerQuoteView,
    QuotePdfRenderError,  # noqa: F401 - 跨层同一错误的公共重导出
)
from shared.schemas.quote_facts import (
    NeedQuantityPreparationStatus as NeedQuantityPreparationStatus,  # noqa: PLC0414 - 同类型公开重导出
)
from shared.schemas.quote_facts import (
    NeedQuotePreparationAssessment as NeedQuotePreparationAssessment,  # noqa: PLC0414 - 同类型公开重导出
)
from shared.schemas.quote_facts import (
    NeedUnitPreparationStatus as NeedUnitPreparationStatus,  # noqa: PLC0414 - 同类型公开重导出
)
from shared.schemas.quote_facts import QuoteEmployeeFact


class QuotePdfRenderer(Protocol):
    """客户报价 PDF 离线渲染端口，不承担授权、存储或发送许可。"""

    def render(self, view: CustomerQuoteView, *, template_version: str) -> bytes:
        """将唯一客户白名单投影渲染为 PDF bytes。"""
        ...


class QuotationActorReader(Protocol):
    """可信当前员工事实读取，不接受客户端角色自证。"""

    async def read_current(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> QuoteEmployeeFact | None:
        """读取当前租户的在职与角色事实。"""
        ...


class QuoteSendReceiptReader(Protocol):
    """锁外读取真实发送记录；未装配不默认成功。"""

    async def read(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId, *, actor_id: EmployeeId
    ) -> QuoteSendReceipt | None:
        """返回实际发送的精确内容绑定，不是PDF下载。"""
        ...


class QuoteCreationSession(Protocol):
    """外层context lease内、freeze前打开的同事务创建会话。"""

    async def preflight(
        self, intent: QuoteCreationIntent, *, operation_id: str | None
    ) -> QuoteDetailView | None:
        """机会锁后先回放历史，再校验CAS；未通过不得freeze。"""
        ...

    async def create_from_basis(
        self, intent: QuoteCreationIntent, basis: QuoteBasis, *, operation_id: str
    ) -> QuoteDetailView:
        """预检完全同载荷才能在当前session写入。"""
        ...


class QuotationVersionService(QuoteFileService, Protocol):
    """新生产候选端口，旧骨架服务不转调本实现。"""
    async def get_issuer(self, tenant_id: TenantId, *, actor: QuotationActor) -> QuoteIssuerPublicView | None:
        """按当前内部角色授权后投影抬头，不扩大原件读权。"""
        ...
    async def approval_snapshot(self, tenant_id: TenantId, quote_id: QuoteId, *, actor: QuotationActor) -> QuoteApprovalSnapshot:
        """当前四成本角色的审批启动快照。"""
        ...

    async def approval_target(self, tenant_id: TenantId, quote_id: QuoteId, *, executor: QuoteWorkflowExecutor) -> QuoteApprovalSnapshot:
        """内部技术入口必须核真实run绑定。"""
        ...

    def open_approval_access(self, tenant_id: TenantId, subject: QuoteApprovalSubject, *, actor_id: EmployeeId,
        action: Literal["read", "decide"]) -> AbstractAsyncContextManager[QuoteApprovalAccessResult]:
        """历史审批读取/决定的当前员工和机会租约。"""
        ...

    def open_approval(self, tenant_id: TenantId, quote_id: QuoteId, *, executor: QuoteWorkflowExecutor) -> AbstractAsyncContextManager[QuoteApprovalSession]:
        """同UoW原子报价审批会话，不接布尔批准。"""
        ...

    async def get_approval_application(self, tenant_id: TenantId, quote_id: QuoteId,
        *, executor: QuoteWorkflowExecutor) -> QuoteApprovalApplicationReceipt | None:
        """历史成功receipt查询，不证明当前文件生成许可。"""
        ...

    def open_creation(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        context: QuoteBusinessContext,
        *,
        actor: QuotationActor,
    ) -> AbstractAsyncContextManager[QuoteCreationSession]:
        """保持报价机会锁跨成本freeze直到本事务提交。"""
        ...

    async def create_from_basis(
        self,
        tenant_id: TenantId,
        intent: QuoteCreationIntent,
        basis: QuoteBasis,
        context: QuoteBusinessContext,
        *,
        operation_id: str,
        actor: QuotationActor,
    ) -> QuoteDetailView:
        """可信已持context lease调用方的便利入口，共用同一session规则。"""
        ...

    async def get(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor: QuotationActor
    ) -> QuoteDetailView:
        """当前内部授权后读历史，不重建最新事实。"""
        ...

    async def list_versions(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: QuotationActor,
    ) -> tuple[QuoteDetailView, ...]:
        """当前内部授权后版本降序读取。"""
        ...

    async def get_by_operation(
        self, tenant_id: TenantId, operation_id: str, *, actor: QuotationActor
    ) -> QuoteDetailView | None:
        """当前内部授权后读操作唯一真实报价。"""
        ...

    async def creation_completion(
        self, tenant_id: TenantId, operation_id: str, *, actor: QuotationActor
    ) -> QuoteCreationCompletion | None:
        """只从持久内容投影真实完成事实。"""
        ...

    async def confirm_issuer(
        self,
        tenant_id: TenantId,
        command: QuoteIssuerCreate,
        *,
        actor: QuotationActor,
        idempotency_key: str,
    ) -> QuoteIssuer:
        """当前老板逐字段手工确认抬头。"""
        ...

    async def get_confirmed_issuer(self, tenant_id: TenantId) -> QuoteIssuer:
        """可信context reader专用，不注册无授权HTTP。"""
        ...

    async def expire_overdue(self, tenant_id: TenantId, *, limit: int) -> int:
        """租户后台作业逐机会短事务过期。"""
        ...

    async def record_verified_send(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        receipt: QuoteSendReceipt,
        *,
        actor: QuotationActor,
    ) -> QuoteDetailView:
        """真实reader回执+approved门禁后原子记录发送。"""
        ...


from domains.quotations.content import (
    build_quote_content,
    format_quote_specification,
    project_customer,
    quote_content_hash,
    to_legacy_quote_view,
    validate_customer_projection,
    validate_quote_basis,
)
from shared.schemas.quote_creation import (
    quote_creation_request_hash,
)


def contains_forbidden_commitment(text: str) -> list[ForbiddenAutoCommitment]:
    """检查文本是否含禁止自动承诺的内容，返回命中的类型。

    模块级纯函数：``guardrails`` 和 ``tool_gateway`` 都要用，
    不该被迫依赖服务实例。

    实现边界：
    - 确定性规则只覆盖已知价格、保证、独家和交期句式；未命中不证明
      自然语言不含承诺，也不能替代逐次人工审批。
    - 允许正文 LF/CRLF、水平制表及首尾空白，拒绝孤立 CR 与其他控制
      字符；长度按原文限制，仅在检查视图中折叠空白，不改写客户原文。
    - 返回全部命中项，不要首个命中就返回：起草者需要一次看到
      所有要改的地方
    """
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 100_000
        or not text.strip()
        or any(
            unicodedata.category(character) == "Cc" and character not in "\n\t"
            for character in text.replace("\r\n", "\n")
        )
    ):
        raise ValidationError("承诺检查文本无效")
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    matched: set[ForbiddenAutoCommitment] = set()
    for category, patterns in _COMMITMENT_PATTERNS.items():
        if any(pattern.search(normalized) is not None for pattern in patterns):
            matched.add(category)
    if _AMBIGUOUS_COMMERCIAL_NUMBER.search(normalized) is not None:
        matched.add(ForbiddenAutoCommitment.FIRST_CONCRETE_PRICE)
    return [category for category in ForbiddenAutoCommitment if category in matched]


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value) for value in values)


_NUMBER = r"(?:\d{1,3}(?:[,.]\d{3})+|\d+(?:[.,]\d+)?)"
_CURRENCY = r"(?:usd|eur|gbp|cny|rmb|jpy|cad|aud|\$|€|£|¥|人民币)"
_WEEKDAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_DELIVERY_TIME = (
    rf"(?:today|tomorrow|tonight|(?:this|next)\s+(?:week|month|{_WEEKDAY})"
    rf"|{_WEEKDAY}|\d{{4}}-\d{{1,2}}-\d{{1,2}})"
)

_COMMITMENT_PATTERNS: dict[
    ForbiddenAutoCommitment,
    tuple[re.Pattern[str], ...],
] = {
    ForbiddenAutoCommitment.FIRST_CONCRETE_PRICE: _patterns(
        rf"(?:{_CURRENCY}\s*{_NUMBER}|{_NUMBER}\s*(?:usd|eur|gbp|cny|rmb|元|美元|欧元))",
        rf"\b(?:price|total|cost)(?:\s*[:=]|\s+(?:is|will\s+be))\s*{_CURRENCY}?\s*{_NUMBER}",
        rf"(?:价格|总价|单价|金额)(?:为|是|:|：)?\s*{_CURRENCY}?\s*{_NUMBER}",
    ),
    ForbiddenAutoCommitment.FORMAL_QUOTATION: _patterns(
        r"\bformal\s+(?:quotation|quote)\b",
        r"(?:正式报价|正式报盘|报价单)",
    ),
    ForbiddenAutoCommitment.DISCOUNT: _patterns(
        rf"{_NUMBER}\s*%\s*(?:discount|off)\b",
        rf"(?:折扣|优惠)(?:为|是|:|：)?\s*{_NUMBER}\s*%",
        rf"{_NUMBER}\s*%\s*(?:折扣|优惠)",
    ),
    ForbiddenAutoCommitment.STOCK_COMMITMENT: _patterns(
        rf"\b(?:have|with)\s+{_NUMBER}\s+(?:units?|pieces?|pcs)\s+in\s+stock\b",
        r"\bin\s+stock\b",
        rf"(?:库存|现货)(?:有|为|是|:|：)?\s*{_NUMBER}",
    ),
    ForbiddenAutoCommitment.DELIVERY_DATE_COMMITMENT: _patterns(
        rf"\b(?:delivery|lead\s+time|ship(?:ment|ping)?)\b.{{0,30}}(?:within|by|in)\s+{_NUMBER}\s+(?:days?|weeks?|months?)\b",
        rf"\b(?:deliver(?:y)?|lead\s+time|ship(?:ment|ping)?|dispatch)\s+"
        rf"(?:(?:is|will\s+be|scheduled\s+for)\s+)?(?:(?:by|on)\s+)?{_DELIVERY_TIME}\b",
        rf"(?:交货期|交期|发货)(?:为|是|:|：|在)?\s*{_NUMBER}\s*(?:天|周|个月|月)(?:内)?",
        r"(?:今天|明天|后天|本周|下周).{0,12}(?:发货|交货|送达)",
        r"(?:签署|签订).{0,12}(?:合同).{0,12}(?:发货|交货)",
    ),
    ForbiddenAutoCommitment.CERTIFICATION_COMMITMENT: _patterns(
        r"\b(?:ce|fda|ul|rohs|iso\s*\d*)\s+(?:certified|approved)\b",
        r"\b(?:is|are|fully)\s+certified\b",
        r"(?:已通过|具备|拥有).{0,12}(?:认证|证书)",
    ),
    ForbiddenAutoCommitment.PAYMENT_TERMS: _patterns(
        r"\bpayment\s+terms?\b",
        r"\bnet\s*\d{1,3}\b",
        r"(?:付款条件|支付条件|账期|预付款|尾款)",
    ),
    ForbiddenAutoCommitment.CONTRACT_TERMS: _patterns(
        r"\b(?:formal\s+)?contract\s+terms?\b",
        r"\bterms\s+and\s+conditions\b",
        r"(?:签署|签订|正式).{0,12}(?:合同|协议)",
    ),
    ForbiddenAutoCommitment.EXCLUSIVE_DISTRIBUTION: _patterns(
        r"\bexclusive\s+(?:distribut(?:or|ion)|agent|agency|rights?)\b",
        r"(?:独家代理|独家经销|独家分销|独家权利)",
    ),
    ForbiddenAutoCommitment.QUALITY_GUARANTEE: _patterns(
        r"\b(?:guarantee|guaranteed|warranty|warrant)\b",
        r"(?:质量保证|品质保证|质保|保修|保证.{0,12}(?:质量|品质|可用|供应))",
    ),
    ForbiddenAutoCommitment.OFF_CATALOG_REFERENCE_PRICE: _patterns(
        rf"(?:目录外|非目录).{{0,30}}(?:参考价|价格).{{0,12}}(?:{_CURRENCY}\s*)?{_NUMBER}",
        rf"\boff[- ]catalog\b.{{0,30}}\b(?:reference|indicative)\s+price\b.{{0,12}}(?:{_CURRENCY}\s*)?{_NUMBER}",
    ),
}

_AMBIGUOUS_COMMERCIAL_NUMBER = re.compile(
    rf"(?:\b(?:commit|guarantee|guaranteed|total\s+will\s+be|moq\s+is)\b|承诺|保证)"
    rf".{{0,30}}(?:{_CURRENCY}\s*)?{_NUMBER}"
    rf"|(?:{_CURRENCY}\s*)?{_NUMBER}.{{0,30}}"
    rf"(?:\b(?:commit|guarantee|guaranteed)\b|承诺|保证)"
)


@runtime_checkable
class QuotationService(Protocol):
    """报价服务。"""

    async def create_draft(
        self, tenant_id: TenantId, request: QuoteCreateRequest
    ) -> QuoteId:
        """创建报价草稿。

        实现要求：
        - 校验成本表：QUOTED 版本、已锁定（``lock_for_quote`` 通过）。
          锁定状态由上层从 costing 域查得传入。
        - 校验每一行的 ``price_snapshot_ref`` 是 QUOTED 基准，
          否则抛 ``IndicativePriceInQuoteError``（第二道门：多查一次
          很便宜，亏本报价很贵）
        - ``valid_until`` 必填且在未来
        - 同一机会已有活跃报价时，新报价自动把旧的转 SUPERSEDED
        """
        ...

    async def submit_for_approval(
        self, tenant_id: TenantId, quote_id: QuoteId, submitted_by: EmployeeId
    ) -> str:
        """提交审批，返回审批包 ID。

        实现要求：
        - 组装审批包：报价内容、成本表摘要、利润率、与利润底线的
          关系、机会上下文——审批人要能**不打开别的页面**就做决定
        - 低于利润底线的报价在审批包里显著标出
        - 审批本身走 ``domains/approvals``；**审批人不能是
          ``prepared_by`` 或机会负责人**（自批禁止）
        """
        ...

    async def apply_approval_result(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        approved: bool,
        decided_by: EmployeeId,
    ) -> None:
        """落审批结果。``ApprovalDecided`` 事件的处理器调用。

        通过 → APPROVED 并发布 ``QuoteApproved``（这是发送门禁）；
        否决 → REJECTED，通知起草人原因。幂等。
        """
        ...

    async def mark_sent(
        self, tenant_id: TenantId, quote_id: QuoteId, message_attempt_ref: str
    ) -> None:
        """记录已发送。只允许 APPROVED → SENT。

        ``message_attempt_ref`` 关联到实际发出的邮件，审计链才完整：
        报价 → 审批 → 邮件，一路可追。
        """
        ...

    async def record_customer_decision(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        accepted: bool,
        feedback: str | None = None,
    ) -> None:
        """记录客户决定。

        ``feedback`` 存客户原话。被拒的报价加上原话是最好的
        改进素材（"price is 15% above our current supplier"
        直接告诉你差距在哪）。
        """
        ...

    async def expire_overdue(self, tenant_id: TenantId) -> int:
        """把过了 ``valid_until`` 的 SENT 报价转 EXPIRED，返回条数。

        ``scheduler_worker`` 定时调。过期要通知负责人——过期报价
        是跟进时机，不只是状态变化。
        """
        ...

    async def get(self, tenant_id: TenantId, quote_id: QuoteId) -> QuoteView: ...

    async def list_versions(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> list[QuoteView]:
        """一个机会的全部报价版本。**含 SUPERSEDED**——
        「我们给这家客户先后报过什么价」是谈判的重要背景。"""
        ...


from shared.schemas.quote_document import customer_quote_hash

__all__ = [
    "ContextQuoteFileScopeAuthorizer",
    "QuotationActorReader",
    "QuotationService",
    "QuotationUowFactory",
    "QuotationVersionService",
    "QuoteApprovalPolicyReader",
    "QuoteApprovalSession",
    "QuoteContextProvider",
    "QuoteCreationSession",
    "QuoteCustomerFileEntry",
    "QuoteCustomerVersionPage",
    "QuoteCustomerVersionView",
    "QuoteCustomerVersionsService",
    "QuoteCustomerVersionsServiceImpl",
    "QuoteFileAccessService",
    "QuoteFileAccessServiceImpl",
    "QuoteFileApprovalFact",
    "QuoteFileApprovalFactsReader",
    "QuoteFileBlockerCode",
    "QuoteFileCurrentFacts",
    "QuoteFileNeedValidator",
    "QuoteFileScopeAuthorizer",
    "QuoteFileScopeFacts",
    "QuoteFileService",
    "QuoteFileView",
    "QuoteFormalFileSnapshot",
    "QuoteGeneratedArtifactFact",
    "QuoteGeneratedArtifactReader",
    "QuoteIssuerReader",
    "QuotePolicySelection",
    "QuotePreparationPolicy",
    "QuoteSendReceiptReader",
    "QuoteWorkflowRunReader",
    "StrictQuotePreparationPolicy",
    "build_quote_content",
    "canonical_quote_specification",
    "contains_forbidden_commitment",
    "customer_quote_hash",
    "format_quote_specification",
    "parse_quote_approval_payload",
    "project_customer",
    "project_quote_approval_display",
    "quote_approval_facts_hash",
    "quote_approval_payload_hash",
    "quote_approval_payloads",
    "quote_change_set_ref",
    "quote_content_hash",
    "quote_context_hash",
    "quote_creation_request_hash",
    "quote_specification",
    "quote_specification_hash",
    "require_quote_approval_access",
    "require_quote_file_scope",
    "required_quote_approvals",
    "to_legacy_quote_view",
    "validate_customer_projection",
    "validate_quote_basis",
]
