"""准备用途只委托需求域公开纯规则，不能从冻结错误猜测缺项。"""

from domains.demand.service import (
    NeedQuoteFacts,
    NeedQuotePreparationAssessment,
    NeedUnitError,
    assess_quote_preparation,
)
from domains.quotations.service import QuoteContextError, QuoteContextUnavailableError


class DemandQuotePreparationProjector:
    """固定异常适配；取消与终止型异常不转成页面缺项。"""

    def project(self, facts: NeedQuoteFacts) -> NeedQuotePreparationAssessment:
        """损坏事实精确转换，意外依赖失败不得伪造成功assessment。"""
        try:
            return assess_quote_preparation(facts)
        except NeedUnitError as exc:
            if exc.code == "facts_corrupt":
                raise QuoteContextError("facts_corrupt") from None
            raise QuoteContextUnavailableError("dependency_unavailable") from None
        except Exception:  # noqa: BLE001 - 未知跨域依赖失败固定脱敏，取消不捕获
            raise QuoteContextUnavailableError("dependency_unavailable") from None
