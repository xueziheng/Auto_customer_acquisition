"""跨域当前事实适配只调用公共服务，不复制需求单位或审批判权。"""

from domains.approvals.service import ApprovalService
from domains.demand.service import NeedUnitError, require_current_unit
from domains.quotations.errors import QuoteFileAccessError
from domains.quotations.schemas import QuoteApprovalFact
from shared.schemas.identifiers import ApprovalId, TenantId
from shared.schemas.provenance import FactualField
from shared.schemas.quote_facts import NeedQuoteFacts
from workflows.quote_approval.approvals import read_quote_facts


class ApprovalServiceQuoteFileFactsReader:
    """复用T5逐字段映射，实时读全部包而非receipt历史state。"""

    def __init__(self, approvals: ApprovalService) -> None:
        self._approvals = approvals

    async def read(
        self, tenant_id: TenantId, approval_ids: tuple[ApprovalId, ...]
    ) -> tuple[QuoteApprovalFact, ...]:
        """依赖故障不转换为空集合，交文件入口固定分类。"""
        return await read_quote_facts(self._approvals, tenant_id, approval_ids)


class DemandQuoteFileNeedValidator:
    """demand唯一当前单位门，不提供raw读取能力。"""

    def require_current_unit(self, facts: NeedQuoteFacts) -> FactualField[str]:
        """缺失/未确认/陈旧均为当前context变化。"""
        try:
            return require_current_unit(facts)
        except NeedUnitError:
            raise QuoteFileAccessError("context_changed") from None
