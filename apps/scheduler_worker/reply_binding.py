"""回复组合的本进程借用资源与一次性客户证据绑定；不拥有连接池或原件。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import BoundedRawArtifactStore
from domains.demand.schemas import (
    CustomerReplyEvidenceClaim,
    VerifiedCustomerReplyEvidence,
)
from domains.demand.service import CustomerReplyEvidenceVerifier
from domains.opportunities.service import OpportunityService
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId


@dataclass(frozen=True)
class ReplyRuntimeResources:
    """仅借用原runtime资源，关闭仍归原inbound与engine owner。"""

    sessions: async_sessionmaker[AsyncSession]
    bounded_raw_store: BoundedRawArtifactStore
    opportunities: OpportunityService
    now: Callable[[], datetime]


class DeferredCustomerReplyEvidenceVerifier:
    """Demand构造先注入；canonical Outreach形成后且ready前只绑定一次。"""

    def __init__(self) -> None:
        self._delegate: CustomerReplyEvidenceVerifier | None = None

    def bind(self, delegate: CustomerReplyEvidenceVerifier) -> None:
        if self._delegate is not None or not isinstance(
            delegate, CustomerReplyEvidenceVerifier
        ):
            raise ValidationError("回复客户证据验证器绑定无效")
        self._delegate = delegate

    async def verify(
        self, tenant_id: TenantId, claim: CustomerReplyEvidenceClaim
    ) -> VerifiedCustomerReplyEvidence:
        if self._delegate is None:
            raise ValidationError("回复客户证据验证器未绑定")
        return await self._delegate.verify(tenant_id, claim)
