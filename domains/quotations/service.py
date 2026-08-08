"""报价域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.quotations.models import ForbiddenAutoCommitment
from domains.quotations.schemas import QuoteCreateRequest, QuoteView
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    QuoteId,
    TenantId,
)


def contains_forbidden_commitment(text: str) -> list[ForbiddenAutoCommitment]:
    """检查文本是否含禁止自动承诺的内容，返回命中的类型。

    模块级纯函数：``guardrails`` 和 ``tool_gateway`` 都要用，
    不该被迫依赖服务实例。

    实现要求：
    - 规则优先（价格模式、"guarantee"、"exclusive"、交期承诺句式等），
      可辅以模型判断，但**模型判定"没有承诺"不能推翻规则判定"有"**
      ——漏放一条承诺的代价远大于误拦一条
    - 返回全部命中项，不要首个命中就返回：起草者需要一次看到
      所有要改的地方
    """
    raise NotImplementedError


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

    async def get(
        self, tenant_id: TenantId, quote_id: QuoteId
    ) -> QuoteView: ...

    async def list_versions(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> list[QuoteView]:
        """一个机会的全部报价版本。**含 SUPERSEDED**——
        「我们给这家客户先后报过什么价」是谈判的重要背景。"""
        ...
