"""报价来源中立读取端口；无业务判权、原件IO或凭证。"""

from typing import Protocol

from shared.schemas.evidence_read import (
    AuthorizedEvidenceReference,
    EvidenceParserCapability,
    EvidenceProfile,
    EvidenceRawContent,
    EvidenceRawMeta,
    EvidenceReadRequest,
    EvidenceScope,
    EvidenceTextResult,
    NeedQuantitySourceFact,
    ParsedEvidenceText,
    QuoteMessageReferenceFact,
)
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    MessageId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.quote_facts import QuoteEmployeeFact


class QuoteEvidenceRawReader(Protocol):
    """元数据与受限bytes分开，以保证授权前零对象IO。"""

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> EvidenceRawMeta: ...
    async def read(
        self, tenant_id: TenantId, artifact_id: ArtifactId, *, maximum_bytes: int
    ) -> EvidenceRawContent: ...


class QuoteEvidenceContextReader(Protocol):
    """按租户读取当前投影；不存在返回None，不补造事实。"""

    async def read_actor(
        self, tenant_id: TenantId, actor_id: EmployeeId
    ) -> QuoteEmployeeFact | None: ...
    async def read_message(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> QuoteMessageReferenceFact | None: ...
    async def read_need_quantity(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedQuantitySourceFact | None: ...


class QuoteEvidenceAccess(Protocol):
    """只验证当前资料ACL与metadata绑定，不读取bytes。"""

    async def authorize(
        self,
        tenant_id: TenantId,
        source_ref: str,
        *,
        actor_id: EmployeeId,
        scope: EvidenceScope,
    ) -> AuthorizedEvidenceReference: ...


class EvidenceTextParser(Protocol):
    """仅成功私有探针可启用的离线受限解析。"""

    def capability(self) -> EvidenceParserCapability: ...
    async def parse(
        self, content: bytes, *, profile: EvidenceProfile, page: int | None
    ) -> ParsedEvidenceText: ...


class QuoteEvidenceReader(Protocol):
    """经过当前授权、Gateway审计和完整性验证的来源读取。"""

    async def read(
        self, tenant_id: TenantId, request: EvidenceReadRequest, *, actor_id: EmployeeId
    ) -> EvidenceTextResult: ...
