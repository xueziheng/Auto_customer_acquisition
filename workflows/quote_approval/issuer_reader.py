"""报价context只读取真实持久老板抬头；不使用样例公司后备。"""

from collections.abc import Callable

from domains.quotations.errors import QuotationUnavailableError
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


class DeferredQuoteIssuerReader:
    """构造环仅由受信本地闭包发布，未绑定不制造默认公司。"""

    def __init__(
        self, quotations: Callable[[], QuotationVersionService | None]
    ) -> None:
        self._quotations = quotations

    async def get_confirmed(self, tenant_id: TenantId) -> QuoteIssuer:
        """沿真实服务返回缺项与故障，不将依赖未发布当作抬头缺失。"""
        quotations = self._quotations()
        if quotations is None:
            raise QuotationUnavailableError("dependency_unavailable")
        return await quotations.get_confirmed_issuer(tenant_id)
