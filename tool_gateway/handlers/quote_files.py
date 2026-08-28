"""报价文件固定用途插件；对象IO只在真实Gateway执行之后。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn, cast

from pydantic import AfterValidator, Field
from pydantic import ValidationError as ModelValidationError

from domains.quotations.errors import (
    QuoteFileAccessError,
    QuoteFileAccessPermissionError,
    QuoteFileAccessUnavailableError,
    QuoteFileError,
    QuoteFilePermissionError,
    QuoteFileUnavailableError,
)
from domains.quotations.schemas import (
    QuoteFileBlockerCode,
    QuoteFileView,
    QuoteFormalFileSnapshot,
)
from domains.quotations.service import (
    QuoteFileAccessService,
    QuoteFileService,
    QuotePdfRenderer,
)
from shared.errors import ValidationError
from shared.schemas.generated_documents import (
    GeneratedDocumentError,
    GeneratedDocumentMeta,
    GeneratedDocumentStore,
)
from shared.schemas.identifiers import (
    EmployeeId,
    IdempotencyKey,
    QuoteFileId,
    QuoteId,
    TenantId,
)
from shared.schemas.quote_document import QuotePdfRenderError
from shared.schemas.quote_facts import fact_text, fact_utc
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.file_rate_limit import (
    ULID,
    Actor,
    CallId,
    FileDTO,
    QuoteFileExecutionHistoryReader,
    QuoteFileRateError,
    QuoteFileRateRequest,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import PreparedToolCall, ToolCallContext
from tool_gateway.quote_file_ledger import QuoteRecoveryAuditError

if TYPE_CHECKING:
    from tool_gateway.handlers.quote_file_recovery import QuoteFileRecoveryHandler

QuoteFileFailureCode = (
    QuoteFileBlockerCode
    | Literal[
        "permission_denied",
        "not_found",
        "invalid_input",
        "storage_inconsistent",
        "dependency_unavailable",
        "lock_timeout",
        "read_limit",
        "reconciliation_required",
        "idempotency_conflict",
        "rate_limited",
        "original_not_found",
        "original_binding_invalid",
        "original_state_changed",
        "metadata_not_found",
        "recovery_unavailable",
        "recovery_audit_binding_invalid",
        "recovery_audit_unavailable",
        "recovery_audit_unknown",
        "invalid_config",
        "template_unsupported",
        "text_limit_exceeded",
        "page_limit_exceeded",
        "byte_limit_exceeded",
        "font_unavailable",
        "unsupported_glyph",
        "layout_failed",
        "render_failed",
    ]
)
FILE_FAILURE_MESSAGES: dict[QuoteFileFailureCode, str] = {
    "quote_inactive": "报价当前不可用于正式文件",
    "quote_expired": "报价已过期",
    "approval_missing": "报价尚无完整批准",
    "approval_invalid": "报价批准无效",
    "approval_expired": "报价批准已过期",
    "decider_invalid": "报价批准人当前无效",
    "context_changed": "报价当前事实已变化",
    "policy_stale": "报价政策已变化",
    "basis_invalid": "报价依据当前无效",
    "permission_denied": "无权使用报价文件",
    "not_found": "报价文件不存在",
    "invalid_input": "报价文件输入无效",
    "storage_inconsistent": "报价文件绑定损坏",
    "dependency_unavailable": "报价文件依赖暂不可用",
    "lock_timeout": "报价文件检查暂忙",
    "read_limit": "报价文件超过读取限制",
    "reconciliation_required": "报价文件结果待核对",
    "idempotency_conflict": "报价文件幂等冲突",
    "rate_limited": "报价文件生成已限速",
    "original_not_found": "原生成调用不存在",
    "original_binding_invalid": "原生成调用绑定无效",
    "original_state_changed": "原生成调用状态已变化",
    "metadata_not_found": "报价文件元数据尚不可恢复",
    "recovery_unavailable": "报价文件恢复暂不可用",
    "recovery_audit_binding_invalid": "恢复审计绑定无效",
    "recovery_audit_unavailable": "恢复审计暂不可用",
    "recovery_audit_unknown": "恢复审计状态待核对",
    "invalid_config": "报价文件渲染配置无效",
    "template_unsupported": "报价文件模板不受支持",
    "text_limit_exceeded": "报价文件文字超过限制",
    "page_limit_exceeded": "报价文件页数超过限制",
    "byte_limit_exceeded": "报价文件大小超过限制",
    "font_unavailable": "报价文件字体不可用",
    "unsupported_glyph": "报价文件字符不受支持",
    "layout_failed": "报价文件排版失败",
    "render_failed": "报价文件渲染失败",
}
FileTool = Literal[
    "quotation.file.generate", "quotation.file.read", "quotation.file.history.read"
]
QuoteIdentifier = Annotated[QuoteId, Field(pattern=rf"^quo_{ULID}$")]
FileIdentifier = Annotated[QuoteFileId, Field(pattern=rf"^qfl_{ULID}$")]


class QuoteFilePreflight(FileDTO):
    """本次检查的完整身份及用途，不跨请求缓存许可。"""

    tenant_id: TenantId = Field(pattern=rf"^tn_{ULID}$")
    user_id: Actor
    actor_id: Annotated[EmployeeId, Field(max_length=40), AfterValidator(fact_text)]
    tool_id: FileTool
    quote_id: QuoteIdentifier
    file_id: FileIdentifier | None
    snapshot: QuoteFormalFileSnapshot | None = Field(repr=False)
    history_file: QuoteFileView | None = Field(repr=False)


class QuoteFilePrepared(FileDTO):
    """只活在本次调用，限速claim由后续真实rate stage填写。"""

    tenant_id: TenantId = Field(pattern=rf"^tn_{ULID}$")
    actor_id: Annotated[EmployeeId, Field(max_length=40), AfterValidator(fact_text)]
    tool_id: FileTool
    quote_id: QuoteIdentifier
    file_id: FileIdentifier | None
    snapshot: QuoteFormalFileSnapshot | None = Field(repr=False)
    history_file: QuoteFileView | None = Field(repr=False)
    rate_claim: QuoteFileRateRequest | None = Field(repr=False)


class QuoteFileBytesPayload(FileDTO):
    """PDF只能经本task一次性交给可信应用。"""

    file: QuoteFileView
    content: bytes = Field(repr=False)


class QuoteFileRecoveryPayload(FileDTO):
    """只证明metadata恢复及所检查时点，不宣称旧账本成功。"""

    file: QuoteFileView
    original_generation_call_id: CallId
    recovery_call_id: CallId
    original_status_at_check: Literal["executing"]
    checked_at: Annotated[datetime, AfterValidator(fact_utc)]


class QuoteFileCallFailure(FileDTO):
    """固定错误码及技术有界Retry-After，不收底层原文。"""

    code: QuoteFileFailureCode
    retry_after_seconds: Annotated[int, Field(ge=1, le=86400)] | None


QuoteFileCallResult = (
    QuoteFileBytesPayload | QuoteFileRecoveryPayload | QuoteFileCallFailure
)


class QuoteFileResultSlotError(ValidationError):
    """槽使用错误固定失败关闭。"""

    code = "invalid_input"

    def __init__(self) -> None:
        super().__init__("文件结果交接无效")


class QuoteFileResultSlot:
    """容量一、task所有权；ContextVar继承不能转移领取权。"""

    def __init__(self) -> None:
        self._value: ContextVar[
            tuple[asyncio.Task[object], QuoteFileCallResult] | None
        ] = ContextVar("quote_file_result", default=None)

    def put(self, result: QuoteFileCallResult) -> None:
        owner = asyncio.current_task()
        if (
            owner is None
            or self._value.get() is not None
            or not isinstance(
                result,
                (QuoteFileBytesPayload, QuoteFileRecoveryPayload, QuoteFileCallFailure),
            )
        ):
            raise QuoteFileResultSlotError()
        self._value.set((owner, result))

    def take(self) -> QuoteFileCallResult | None:
        value = self._value.get()
        if value is None:
            return None
        if value[0] is not asyncio.current_task():
            raise QuoteFileResultSlotError()
        self._value.set(None)
        return value[1]

    def clear(self) -> None:
        value = self._value.get()
        if value is not None and value[0] is asyncio.current_task():
            self._value.set(None)


def file_failure_category(code: QuoteFileFailureCode) -> ToolErrorCategory:
    """插件固定分类，不扩大通用Gateway错误枚举。"""
    if code == "permission_denied":
        return ToolErrorCategory.PERMISSION_DENIED
    if code in {
        "quote_inactive",
        "quote_expired",
        "approval_missing",
        "approval_invalid",
        "approval_expired",
        "decider_invalid",
        "context_changed",
        "policy_stale",
        "basis_invalid",
    }:
        return ToolErrorCategory.APPROVAL_REQUIRED
    if code in {
        "reconciliation_required",
        "original_state_changed",
        "metadata_not_found",
        "recovery_audit_unknown",
    }:
        return ToolErrorCategory.RECONCILIATION_REQUIRED
    if code in {
        "dependency_unavailable",
        "lock_timeout",
        "recovery_unavailable",
        "recovery_audit_unavailable",
    }:
        return ToolErrorCategory.PROVIDER_TRANSIENT
    if code == "rate_limited":
        return ToolErrorCategory.RATE_LIMITED
    if code == "idempotency_conflict":
        return ToolErrorCategory.IDEMPOTENCY_CONFLICT
    return ToolErrorCategory.VALIDATION


def fail_file(
    slot: QuoteFileResultSlot,
    code: QuoteFileFailureCode,
    retry: int | None = None,
    *,
    category: ToolErrorCategory | None = None,
) -> NoReturn:
    """失败替换任何尚未交出的bytes，原文永不外泄。"""
    slot.clear()
    slot.put(QuoteFileCallFailure(code=code, retry_after_seconds=retry))
    raise ToolGatewayError(
        category or file_failure_category(code), retry_after_seconds=retry
    )


def quote_file_failure_code(
    error: Exception, *, write_attempted: bool = False
) -> QuoteFileFailureCode:
    """只识别明确typed异常；未知错误不读取str或任意code属性。"""
    if isinstance(error, QuoteRecoveryAuditError):
        return error.code
    if isinstance(error, (QuoteFileAccessPermissionError, QuoteFilePermissionError)):
        return "permission_denied"
    if isinstance(error, QuoteFileAccessError):
        return error.code
    if isinstance(error, (QuoteFileAccessUnavailableError, QuoteFileUnavailableError)):
        if error.code == "storage_unknown" or (
            write_attempted and error.code == "dependency_unavailable"
        ):
            return "reconciliation_required"
        return error.code
    if isinstance(error, QuoteFileError):
        mapped: dict[str, QuoteFileFailureCode] = {
            "file_conflict": "idempotency_conflict",
            "metadata_mismatch": "storage_inconsistent",
            "workflow_binding_invalid": "storage_inconsistent",
        }
        return mapped.get(error.code, cast(QuoteFileFailureCode, error.code))
    if isinstance(error, GeneratedDocumentError):
        return {
            "not_found": "not_found",
            "invalid_binding": "storage_inconsistent",
            "corrupt": "storage_inconsistent",
            "conflict": "idempotency_conflict",
            "unavailable": "reconciliation_required"
            if write_attempted
            else "dependency_unavailable",
            "commit_unknown": "reconciliation_required",
            "read_limit": "read_limit",
            "bounded_unavailable": "dependency_unavailable",
        }[error.code]
    if isinstance(error, QuotePdfRenderError):
        return error.code
    if isinstance(error, (ValidationError, ModelValidationError)):
        return "reconciliation_required" if write_attempted else "invalid_input"
    return "reconciliation_required" if write_attempted else "dependency_unavailable"


@contextmanager
def file_call_errors(
    slot: QuoteFileResultSlot, *, writing: Callable[[], bool] = lambda: False
) -> Iterator[None]:
    """取消不改分类，所有其他失败只通过同一个结果槽。"""
    try:
        yield
    except asyncio.CancelledError:
        slot.clear()
        raise
    except ToolGatewayError:
        raise
    except QuoteFileRateError as error:
        fail_file(
            slot,
            "reconciliation_required"
            if error.code == "claim_invalid"
            else "dependency_unavailable",
        )
    except Exception as error:  # noqa: BLE001 -- 安全错误边界
        fail_file(slot, quote_file_failure_code(error, write_attempted=writing()))


def same_quote_file_snapshot(
    a: QuoteFormalFileSnapshot, b: QuoteFormalFileSnapshot
) -> bool:
    """只排除检查时钟；完整客户投影与每个业务绑定都必须相等。"""
    return (
        a.tenant_id,
        a.quote_id,
        a.opportunity_id,
        a.quote_version,
        a.quote_content_hash,
        a.customer_content_hash,
        a.approval_run_id,
        a.approval_facts_hash,
        a.template_version,
        a.customer,
    ) == (
        b.tenant_id,
        b.quote_id,
        b.opportunity_id,
        b.quote_version,
        b.quote_content_hash,
        b.customer_content_hash,
        b.approval_run_id,
        b.approval_facts_hash,
        b.template_version,
        b.customer,
    )


def quote_file_generation_key(snapshot: QuoteFormalFileSnapshot) -> IdempotencyKey:
    """Store和ledger共享同一canonical，actor不参与。"""
    return IdempotencyKey(
        f"{snapshot.quote_id}:{snapshot.quote_version}:quote_pdf:{snapshot.template_version}"
    )


def quote_file_generation_parts(snapshot: QuoteFormalFileSnapshot) -> tuple[bytes, ...]:
    """固定协议顺序；其版本不是HMAC密钥版本。"""
    return tuple(
        part.encode("utf-8")
        for part in (
            "quote-file-v1",
            snapshot.tenant_id,
            "quotation.file.generate",
            snapshot.quote_id,
            str(snapshot.quote_version),
            snapshot.template_version,
            snapshot.quote_content_hash,
            snapshot.customer_content_hash,
            snapshot.approval_run_id,
            snapshot.approval_facts_hash,
        )
    )


def require_file_binding(
    file: QuoteFileView, snapshot: QuoteFormalFileSnapshot
) -> None:
    """T6已核receipt及artifact hash，本处核正式投影与模板。"""
    if (
        file.quote_id,
        file.quote_version,
        file.quote_content_hash,
        file.customer_content_hash,
        file.template_version,
    ) != (
        snapshot.quote_id,
        snapshot.quote_version,
        snapshot.quote_content_hash,
        snapshot.customer_content_hash,
        snapshot.template_version,
    ):
        raise GeneratedDocumentError("invalid_binding")


def require_document_binding(
    meta: GeneratedDocumentMeta,
    snapshot: QuoteFormalFileSnapshot,
    file: QuoteFileView | None = None,
) -> None:
    """只比较技术归属，业务许可由domain每次重查。"""
    if (
        meta.tenant_id,
        meta.kind,
        meta.mime_type,
        meta.subject_ref,
        meta.sequence_number,
        meta.workflow_run_id,
        meta.generated_by,
        meta.idempotency_key,
    ) != (
        snapshot.tenant_id,
        "quote_pdf",
        "application/pdf",
        snapshot.quote_id,
        snapshot.quote_version,
        snapshot.approval_run_id,
        snapshot.template_version,
        quote_file_generation_key(snapshot),
    ):
        raise GeneratedDocumentError("invalid_binding")
    if file is not None and (
        meta.artifact_id,
        meta.artifact_hash,
        meta.size_bytes,
        meta.generated_at,
    ) != (file.artifact_id, file.content_hash, file.size_bytes, file.generated_at):
        raise GeneratedDocumentError("invalid_binding")


async def formal_file_postcheck(
    access: QuoteFileAccessService,
    files: QuoteFileService,
    snapshot: QuoteFormalFileSnapshot,
    file_id: QuoteFileId,
    *,
    actor_id: EmployeeId,
) -> QuoteFileView:
    """写锁外重新取得T6及完整正式许可，不删除已提交文件。"""
    file = await files.get_file(
        snapshot.tenant_id, snapshot.quote_id, file_id, actor_id=actor_id
    )
    latest = await access.authorize(
        snapshot.tenant_id, snapshot.quote_id, actor_id=actor_id
    )
    if not same_quote_file_snapshot(snapshot, latest):
        raise GeneratedDocumentError("conflict")
    require_file_binding(file, latest)
    return file


def _input(names: tuple[str, ...]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(names),
        "properties": {
            name: {
                "type": "string",
                "pattern": rf"^{ {'quote_id': 'quo', 'file_id': 'qfl', 'original_generation_call_id': 'tcl'}[name] }_{ULID}$",
            }
            for name in names
        },
    }


def _manifest(
    tool: str, *, history: bool = False, generate: bool = False, recovery: bool = False
) -> ToolManifest:
    output: dict[str, object] = {
        "provider_ref": {"type": "string", "pattern": rf"^qfl_{ULID}$"}
    }
    params = (
        ("quote_id",)
        if generate
        else ("quote_id", "original_generation_call_id" if recovery else "file_id")
    )
    permission = (
        "file_generate"
        if generate
        else "file_reconcile"
        if recovery
        else "file_history_read"
        if history
        else "file_read"
    )
    return ToolManifest(
        tool_id=tool,
        version="v1",
        description="报价文件受控用途",
        risk_level=RiskLevel.MEDIUM if generate or recovery else RiskLevel.LOW,
        cost_class=CostClass.FREE,
        requires_approval=not history,
        idempotency=IdempotencyRequirement.REQUIRED
        if generate
        else IdempotencyRequirement.NONE,
        required_permissions=(f"quotation:{permission}",),
        checks=("tenant", "permission")
        + (() if history else ("approval",))
        + (("idempotency", "rate_limit") if generate else ()),
        input_schema=_input(params),
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": list(output),
            "properties": output,
        },
    )


GENERATE_MANIFEST = _manifest("quotation.file.generate", generate=True)
READ_MANIFEST = _manifest("quotation.file.read")
HISTORY_MANIFEST = _manifest("quotation.file.history.read", history=True)
RECOVERY_MANIFEST = _manifest("quotation.file.reconcile", recovery=True)


def require_file_preflight(
    ctx: ToolCallContext, preflight: object, tool_id: FileTool
) -> QuoteFilePreflight:
    """逐项核真实上下文，不允许调用者字段替代当前检查。"""
    if (
        not isinstance(preflight, QuoteFilePreflight)
        or (
            ctx.tenant_id,
            ctx.user_id,
            ctx.tool_id,
            ctx.params.get("quote_id"),
            ctx.params.get("file_id"),
        )
        != (
            preflight.tenant_id,
            preflight.actor_id,
            tool_id,
            preflight.quote_id,
            preflight.file_id,
        )
        or preflight.user_id != ctx.user_id
        or preflight.tool_id != tool_id
    ):
        raise ValidationError("文件准备绑定无效")
    if any(
        value is not None for value in (ctx.run_id, ctx.approval_ref, ctx.campaign_ref)
    ):
        raise ValidationError("文件准备绑定无效")
    history = tool_id == "quotation.file.history.read"
    if history:
        valid = (
            preflight.history_file is not None
            and preflight.file_id is not None
            and preflight.snapshot is None
            and ctx.idempotency_key is None
        )
    else:
        valid = (
            preflight.snapshot is not None
            and preflight.history_file is None
            and ((preflight.file_id is None) == (tool_id == "quotation.file.generate"))
        )
    if not valid:
        raise ValidationError("文件用途无效")
    return preflight


class QuoteFileGenerateHandler:
    """首次真实执行才可render；之后只有metadata恢复。"""

    def __init__(
        self,
        access: QuoteFileAccessService,
        files: QuoteFileService,
        store: GeneratedDocumentStore,
        renderer: QuotePdfRenderer,
        history_reader: QuoteFileExecutionHistoryReader,
        fingerprints: HmacFingerprintProvider,
        slot: QuoteFileResultSlot,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._access, self._files, self._store, self._renderer = (
            access,
            files,
            store,
            renderer,
        )
        self._history, self._fingerprints, self._slot, self._now = (
            history_reader,
            fingerprints,
            slot,
            now,
        )

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        with file_call_errors(self._slot):
            p = require_file_preflight(ctx, preflight, "quotation.file.generate")
            latest = await self._access.authorize(
                p.tenant_id, p.quote_id, actor_id=p.actor_id
            )
            if (
                p.snapshot is None
                or not same_quote_file_snapshot(p.snapshot, latest)
                or ctx.idempotency_key != quote_file_generation_key(latest)
            ):
                fail_file(self._slot, "idempotency_conflict")
            digest, version = self._fingerprints.fingerprint(
                quote_file_generation_parts(latest)
            )
            return PreparedToolCall(
                digest,
                version,
                {},
                QuoteFilePrepared(
                    tenant_id=p.tenant_id,
                    actor_id=p.actor_id,
                    tool_id=p.tool_id,
                    quote_id=p.quote_id,
                    file_id=None,
                    snapshot=latest,
                    history_file=None,
                    rate_claim=None,
                ),
            )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str]:
        return await self._execute(tenant_id, prepared, metadata_only=False)

    async def reconcile(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str]:
        """通用重试hook永不生成、put或delete。"""
        return await self._execute(tenant_id, prepared, metadata_only=True)

    async def _execute(
        self, tenant: TenantId, prepared: PreparedToolCall, *, metadata_only: bool
    ) -> Mapping[str, str]:
        attempted = False
        with file_call_errors(self._slot, writing=lambda: attempted or metadata_only):
            p = prepared.payload
            if (
                not isinstance(p, QuoteFilePrepared)
                or p.tenant_id != tenant
                or p.tool_id != "quotation.file.generate"
                or p.snapshot is None
                or p.rate_claim is None
            ):
                fail_file(self._slot, "invalid_input")
            snapshot = await self._access.authorize(
                tenant, p.quote_id, actor_id=p.actor_id
            )
            if not same_quote_file_snapshot(p.snapshot, snapshot):
                fail_file(self._slot, "idempotency_conflict")
            prior = await self._history.has_prior_execution(tenant, p.rate_claim)
            metadata_only = metadata_only or prior
            existing = tuple(
                f
                for f in await self._files.list_files(
                    tenant, p.quote_id, actor_id=p.actor_id
                )
                if f.template_version == snapshot.template_version
            )
            if len(existing) > 1:
                fail_file(self._slot, "storage_inconsistent")
            meta = await self._store.get_meta_by_key(
                tenant, quote_file_generation_key(snapshot)
            )
            if meta is not None:
                require_document_binding(
                    meta, snapshot, existing[0] if existing else None
                )
            if existing:
                require_file_binding(existing[0], snapshot)
                if meta is None:
                    fail_file(self._slot, "storage_inconsistent")
                file = existing[0]
            else:
                if meta is None:
                    if metadata_only:
                        fail_file(self._slot, "reconciliation_required")
                    content = self._renderer.render(
                        snapshot.customer, template_version=snapshot.template_version
                    )
                    attempted = True
                    meta = await self._store.put_pdf(
                        tenant,
                        content,
                        workflow_run_id=snapshot.approval_run_id,
                        subject_ref=p.quote_id,
                        sequence_number=snapshot.quote_version,
                        idempotency_key=quote_file_generation_key(snapshot),
                        generated_by=snapshot.template_version,
                    )
                    require_document_binding(meta, snapshot)
                attempted = True
                file = await self._files.record_file(
                    tenant, p.quote_id, meta.artifact_id, actor_id=p.actor_id
                )
                require_document_binding(meta, snapshot, file)
            file = await formal_file_postcheck(
                self._access, self._files, snapshot, file.file_id, actor_id=p.actor_id
            )
            return {"provider_ref": file.file_id}


class QuoteFileReadHandler:
    """构造时固定正式/历史用途，有界读取前后各自检查。"""

    def __init__(
        self,
        access: QuoteFileAccessService,
        files: QuoteFileService,
        store: GeneratedDocumentStore,
        slot: QuoteFileResultSlot,
        fingerprints: HmacFingerprintProvider,
        *,
        maximum_bytes: int,
        history: bool,
    ) -> None:
        if (
            type(maximum_bytes) is not int
            or maximum_bytes <= 0
            or type(history) is not bool
        ):
            raise ValidationError("文件读取配置无效")
        self._access, self._files, self._store, self._slot, self._fingerprints = (
            access,
            files,
            store,
            slot,
            fingerprints,
        )
        self._maximum, self._history = maximum_bytes, history
        self._tool: FileTool = (
            "quotation.file.history.read" if history else "quotation.file.read"
        )

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        with file_call_errors(self._slot):
            p = require_file_preflight(ctx, preflight, self._tool)
            if ctx.idempotency_key is not None:
                fail_file(self._slot, "invalid_input")
            digest, version = self._fingerprints.fingerprint(
                tuple(
                    s.encode()
                    for s in (
                        "quote-file-read-v1",
                        p.tenant_id,
                        self._tool,
                        p.quote_id,
                        p.file_id or "",
                    )
                )
            )
            return PreparedToolCall(
                digest,
                version,
                {},
                QuoteFilePrepared(
                    tenant_id=p.tenant_id,
                    actor_id=p.actor_id,
                    tool_id=p.tool_id,
                    quote_id=p.quote_id,
                    file_id=p.file_id,
                    snapshot=p.snapshot,
                    history_file=p.history_file,
                    rate_claim=None,
                ),
            )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str]:
        with file_call_errors(self._slot):
            p = prepared.payload
            if (
                not isinstance(p, QuoteFilePrepared)
                or p.tenant_id != tenant_id
                or p.tool_id != self._tool
                or p.file_id is None
                or p.rate_claim is not None
            ):
                fail_file(self._slot, "invalid_input")
            if self._history:
                file = await self._access.authorize_history(
                    tenant_id, p.quote_id, p.file_id, actor_id=p.actor_id
                )
                if file != p.history_file or p.snapshot is not None:
                    fail_file(self._slot, "storage_inconsistent")
            else:
                if p.snapshot is None or p.history_file is not None:
                    fail_file(self._slot, "invalid_input")
                file = await formal_file_postcheck(
                    self._access,
                    self._files,
                    p.snapshot,
                    p.file_id,
                    actor_id=p.actor_id,
                )
            approval = await self._files.get_file_approval(
                tenant_id, p.quote_id, actor_id=p.actor_id
            )
            if approval is None:
                fail_file(self._slot, "storage_inconsistent")
            payload = await self._store.get_bounded(
                tenant_id, file.artifact_id, maximum_bytes=self._maximum
            )
            meta = payload.meta
            if (
                meta.tenant_id,
                meta.artifact_id,
                meta.artifact_hash,
                meta.size_bytes,
                meta.generated_at,
                meta.kind,
                meta.mime_type,
                meta.workflow_run_id,
                meta.subject_ref,
                meta.sequence_number,
                meta.generated_by,
                meta.idempotency_key,
            ) != (
                tenant_id,
                file.artifact_id,
                file.content_hash,
                file.size_bytes,
                file.generated_at,
                "quote_pdf",
                "application/pdf",
                approval.approval_run_id,
                file.quote_id,
                file.quote_version,
                file.template_version,
                f"{file.quote_id}:{file.quote_version}:quote_pdf:{file.template_version}",
            ):
                fail_file(self._slot, "storage_inconsistent")
            if len(payload.content) > self._maximum:
                fail_file(self._slot, "read_limit")
            if (
                len(payload.content) != file.size_bytes
                or hashlib.sha256(payload.content).hexdigest() != file.content_hash
            ):
                fail_file(self._slot, "storage_inconsistent")
            if self._history:
                latest = await self._access.authorize_history(
                    tenant_id, p.quote_id, p.file_id, actor_id=p.actor_id
                )
            else:
                if p.snapshot is None:
                    fail_file(self._slot, "invalid_input")
                latest = await formal_file_postcheck(
                    self._access,
                    self._files,
                    p.snapshot,
                    p.file_id,
                    actor_id=p.actor_id,
                )
            if latest != file:
                fail_file(self._slot, "storage_inconsistent")
            self._slot.put(QuoteFileBytesPayload(file=file, content=payload.content))
            return {"provider_ref": file.file_id}


def register_quote_file_tools(
    registry: ToolRegistry,
    *,
    generate: QuoteFileGenerateHandler,
    read: QuoteFileReadHandler,
    history: QuoteFileReadHandler,
    recovery: QuoteFileRecoveryHandler,
) -> None:
    """只注册四固定实例，不触旧email/DNS工具集合。"""
    for manifest, handler in (
        (GENERATE_MANIFEST, generate),
        (READ_MANIFEST, read),
        (HISTORY_MANIFEST, history),
        (RECOVERY_MANIFEST, recovery),
    ):
        registry.register(manifest, handler)
