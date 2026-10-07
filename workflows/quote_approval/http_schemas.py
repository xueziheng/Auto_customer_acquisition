"""报价HTTP技术结果；不复制业务报价或Gateway调用身份。"""

from typing import Annotated

from pydantic import Field

from shared.schemas.evidence_read import EvidenceProfile, EvidenceScope
from shared.schemas.identifiers import ArtifactId, QuoteId, RunId
from shared.schemas.quote_creation import QuoteDTO
from shared.schemas.quote_facts import FactHash


class QuoteApprovalStartResult(QuoteDTO):
    """只表示真实工作流已启动并完成绑定核验，不表示批准。"""

    quote_id: QuoteId
    run_id: RunId


class EvidencePreviewPublicView(QuoteDTO):
    """仅授权原文端点允许领取正文，不含对象地址或自由metadata。"""

    source_ref: str
    scope: EvidenceScope
    artifact_id: ArtifactId
    raw_hash: FactHash
    profile: EvidenceProfile
    page: Annotated[int, Field(gt=0)] | None
    text: str = Field(repr=False)
    text_hash: FactHash


class EvidenceLocatorPublicView(EvidencePreviewPublicView):
    """本次原件与正文hash绑定的已核验选区。"""

    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    excerpt_hash: FactHash
    excerpt: str = Field(repr=False)
    locator: str
