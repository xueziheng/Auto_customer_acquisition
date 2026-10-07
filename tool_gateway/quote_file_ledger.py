"""文件插件的账本窄投影；只使用公共UoW，不寻找或完成原调用。"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator, Field

from shared.errors import TradeOSError
from shared.schemas.identifiers import EmployeeId, QuoteId, TenantId
from shared.schemas.quote_facts import fact_text
from tool_gateway.errors import ToolCallStatus
from tool_gateway.file_rate_limit import ULID, Actor, CallId, FileDTO, Hash, Key, Label
from tool_gateway.repository import (
    ToolCallEventRecord,
    ToolCallId,
    ToolCallRecord,
    ToolGatewayUnitOfWorkFactory,
)


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
        return _generation_fact(row)


def _generation_fact(row: ToolCallRecord) -> QuoteGenerationLedgerFact:
    """审计与只读adapter复用同一逐字段投影。"""
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


class QuoteRecoveryAuditError(TradeOSError):
    """审计结果固定分类，未知commit不得继续record_file。"""

    def __init__(
        self,
        code: Literal[
            "recovery_audit_binding_invalid",
            "recovery_audit_unavailable",
            "recovery_audit_unknown",
        ],
    ) -> None:
        messages = {
            "recovery_audit_binding_invalid": "恢复审计绑定无效",
            "recovery_audit_unavailable": "恢复审计暂不可用",
            "recovery_audit_unknown": "恢复审计状态待核对",
        }
        if code not in messages:
            raise ValueError("恢复审计错误码无效")
        self.code = code
        super().__init__(messages[code])


class PublicLedgerQuoteRecoveryAudit:
    """仅public get/append；新call前置requested，不写原call或原events。"""

    def __init__(
        self,
        uow_factory: ToolGatewayUnitOfWorkFactory,
        *,
        now: Callable[[], datetime],
        id_factory: Callable[[str], str],
    ) -> None:
        self._uows, self._now, self._id = uow_factory, now, id_factory

    async def append_requested(
        self,
        tenant_id: TenantId,
        *,
        preflight: QuoteFileRecoveryPreflight,
        original: QuoteGenerationLedgerFact,
        request_fingerprint: str,
        fingerprint_version: str,
    ) -> None:
        """没有旧call锁或fence；所有检查均为当前plain SELECT时点。"""
        committing = False
        try:
            if (
                not isinstance(preflight, QuoteFileRecoveryPreflight)
                or not isinstance(original, QuoteGenerationLedgerFact)
                or (
                    tenant_id != preflight.tenant_id
                    or tenant_id != original.tenant_id
                    or preflight.user_id != preflight.actor_id
                    or preflight.original_generation_call_id != original.call_id
                    or preflight.recovery_call_id == original.call_id
                )
            ):
                raise QuoteRecoveryAuditError("recovery_audit_binding_invalid")
            async with self._uows(tenant_id) as uow:
                current = await uow.calls.get(tenant_id, preflight.recovery_call_id)
                old = await uow.calls.get(tenant_id, original.call_id)
                if current is None or (
                    current.tenant_id,
                    current.tool_call_id,
                    current.tool_id,
                    current.tool_version,
                    current.status,
                    current.duplicate_of,
                    current.provider_ref,
                    current.idempotency_key,
                    current.request_fingerprint,
                    current.fingerprint_version,
                    current.user_id,
                    current.run_id,
                    current.campaign_id,
                    current.message_attempt_id,
                ) != (
                    tenant_id,
                    preflight.recovery_call_id,
                    "quotation.file.reconcile",
                    preflight.recovery_tool_version,
                    ToolCallStatus.EXECUTING,
                    None,
                    None,
                    f"call:{preflight.recovery_call_id}",
                    request_fingerprint,
                    fingerprint_version,
                    preflight.user_id,
                    None,
                    None,
                    None,
                ):
                    raise QuoteRecoveryAuditError("recovery_audit_binding_invalid")
                if (
                    old is None
                    or _generation_fact(old) != original
                    or old.status != ToolCallStatus.EXECUTING
                    or old.provider_ref is not None
                ):
                    raise QuoteRecoveryAuditError("recovery_audit_binding_invalid")
                await uow.calls.append_event(
                    ToolCallEventRecord(
                        tenant_id=tenant_id,
                        event_id=self._id("tce"),
                        tool_call_id=preflight.recovery_call_id,
                        stage="recovery",
                        outcome="requested",
                        rule=f"original:{original.call_id}",
                        category=None,
                        actor_id=preflight.user_id,
                        run_id=None,
                        campaign_id=None,
                        message_attempt_id=None,
                        occurred_at=self._now(),
                        duration_ms=0,
                        cost_note=None,
                    )
                )
                committing = True
        except QuoteRecoveryAuditError:
            if committing:
                raise QuoteRecoveryAuditError("recovery_audit_unknown") from None
            raise
        except Exception:  # noqa: BLE001 -- 原文/SQL不进入固定错误
            raise QuoteRecoveryAuditError(
                "recovery_audit_unknown" if committing else "recovery_audit_unavailable"
            ) from None
