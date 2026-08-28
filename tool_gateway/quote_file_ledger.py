"""文件插件的账本窄投影；只使用公共UoW，不寻找或完成原调用。"""

from typing import Annotated, Protocol

from pydantic import AfterValidator, Field

from shared.schemas.identifiers import EmployeeId, QuoteId, TenantId
from shared.schemas.quote_facts import fact_text
from tool_gateway.errors import ToolCallStatus
from tool_gateway.file_rate_limit import ULID, Actor, CallId, FileDTO, Hash, Key, Label
from tool_gateway.repository import ToolCallId, ToolGatewayUnitOfWorkFactory


class QuoteGenerationLedgerFact(FileDTO):
    """保留真实状态及原生成摘要；不包含可变更方法。"""

    tenant_id: TenantId = Field(pattern=rf"^tn_{ULID}$")
    call_id: CallId
    tool_id: Label
    tool_version: Label
    idempotency_key: Key | None
    request_fingerprint: Hash | None
    fingerprint_version: Label | None
    status: ToolCallStatus
    provider_ref: str | None


class QuoteFileRecoveryPreflight(FileDTO):
    """NONE工具的新received ID就是其canonical，不用于生成工具。"""

    tenant_id: TenantId = Field(pattern=rf"^tn_{ULID}$")
    user_id: Actor
    actor_id: Annotated[EmployeeId, Field(max_length=40), AfterValidator(fact_text)]
    quote_id: QuoteId = Field(pattern=rf"^quo_{ULID}$")
    original_generation_call_id: CallId
    recovery_call_id: CallId
    recovery_tool_version: Label


class QuoteGenerationLedgerReader(Protocol):
    """按明确ID读取同tenant原调用；无find_by_key。"""

    async def read(
        self, tenant_id: TenantId, call_id: ToolCallId
    ) -> QuoteGenerationLedgerFact | None: ...


class QuoteRecoveryAudit(Protocol):
    """确定提交请求审计之后才可补文件关联。"""

    async def append_requested(
        self,
        tenant_id: TenantId,
        *,
        preflight: QuoteFileRecoveryPreflight,
        original: QuoteGenerationLedgerFact,
        request_fingerprint: str,
        fingerprint_version: str,
    ) -> None: ...


class PublicLedgerQuoteGenerationReader:
    """逐字段投影公开record，不持有repository私有接口。"""

    def __init__(self, uow_factory: ToolGatewayUnitOfWorkFactory) -> None:
        self._uows = uow_factory

    async def read(
        self, tenant_id: TenantId, call_id: ToolCallId
    ) -> QuoteGenerationLedgerFact | None:
        async with self._uows(tenant_id) as uow:
            row = await uow.calls.get(tenant_id, call_id)
        if row is None:
            return None
        return QuoteGenerationLedgerFact(
            tenant_id=row.tenant_id,
            call_id=row.tool_call_id,
            tool_id=row.tool_id,
            tool_version=row.tool_version,
            idempotency_key=row.idempotency_key,
            request_fingerprint=row.request_fingerprint,
            fingerprint_version=row.fingerprint_version,
            status=row.status,
            provider_ref=row.provider_ref,
        )
