"""本API进程的显式报价依赖束；不复用手工发送Gateway。"""

from dataclasses import dataclass
from typing import Protocol

from connectors.evidence_text.client import LinuxEvidenceTextParser
from domains.approvals.service import QuoteApprovalAccess
from domains.costing.service import (
    CostingFreezeService,
    CostingQuoteService,
    CostScopeSourceAccess,
    PricingEvidenceReader,
)
from domains.demand.service import NeedUnitEvidenceReader, NeedUnitService
from domains.quotations.service import (
    QuotationVersionService,
    QuoteContextProvider,
    QuoteCustomerVersionsService,
    QuoteFileService,
    QuotePreparationReadService,
)
from shared.evidence_read import QuoteEvidenceReader
from workflows.quote_approval.application import (
    QuoteApplicationService,
    QuotePreparationApplication,
)
from workflows.quote_approval.files import QuoteFilesApplication
from workflows.quote_approval.http import QuoteApprovalStarter


class QuotationRuntimeLifecycle(Protocol):
    """同一parser的进程启停端口，HTTP不调用probe。"""

    async def startup(self) -> None: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class QuotationDomainComposition:
    """来源已经显式注入的真实域服务，文件组可独立禁用。"""

    context_provider: QuoteContextProvider
    quotations: QuotationVersionService
    costing_quotes: CostingQuoteService
    costing_freeze: CostingFreezeService
    need_units: NeedUnitService
    creation: QuoteApplicationService
    preparation: QuotePreparationApplication
    preparation_reads: QuotePreparationReadService
    files: QuoteFileService | None
    approval_access: QuoteApprovalAccess


@dataclass(frozen=True)
class QuotationEvidenceComposition:
    """独立来源工具及其受限解析器，不持报价或旧发送服务。"""

    pricing: PricingEvidenceReader
    need_units: NeedUnitEvidenceReader
    scope_access: CostScopeSourceAccess
    preview_reader: QuoteEvidenceReader
    parser: LinuxEvidenceTextParser


@dataclass(frozen=True)
class QuotationHttpComposition:
    """发布后不可替换的API能力；None只表示明确禁用的文件组。"""

    domain: QuotationDomainComposition
    approval_starter: QuoteApprovalStarter
    customer_versions: QuoteCustomerVersionsService | None
    files_application: QuoteFilesApplication | None
    evidence: QuotationEvidenceComposition
    lifecycle: QuotationRuntimeLifecycle
