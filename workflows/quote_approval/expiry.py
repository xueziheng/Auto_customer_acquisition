"""持锁scheduler的报价到期入口，只委托现有域状态机。"""

from domains.quotations.service import QuotationVersionService
from shared.schemas.identifiers import TenantId


class QuoteExpiryDriver:
    """无actor、审批回执或新run；批量上限来自显式core配置。"""

    def __init__(
        self, quotations: QuotationVersionService, tenant_id: TenantId, *, limit: int
    ) -> None:
        self._quotations, self._tenant_id, self._limit = quotations, tenant_id, limit

    async def scan_once(self) -> int:
        """沿域既有expire_overdue规则，错误与取消原样交还worker。"""
        return await self._quotations.expire_overdue(self._tenant_id, limit=self._limit)
