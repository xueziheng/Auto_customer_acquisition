"""报价context只读取真实持久老板抬头；不使用样例公司后备。"""

from domains.quotations.schemas import QuoteIssuer
from domains.quotations.service import QuotationVersionService
from shared.schemas.identifiers import TenantId


class PersistentQuoteIssuerReader:
    """通过公开服务读取当前确认，旧报价仍保留其原快照。"""

    def __init__(self, quotations: QuotationVersionService) -> None:
        """真实报价服务必须显式装配。"""
        self._quotes = quotations

    async def get_confirmed(self, tenant_id: TenantId) -> QuoteIssuer:
        """缺确认向上传递固定错误，不产生假抬头。"""
        return await self._quotes.get_confirmed_issuer(tenant_id)
