"""有限metadata恢复：新调用审计，旧调用只读，不持宽Store或renderer。"""

from collections.abc import Callable, Mapping
from datetime import datetime

from pydantic import Field

from domains.quotations.schemas import QuoteFormalFileSnapshot
from domains.quotations.service import QuoteFileAccessService, QuoteFileService
from shared.schemas.generated_documents import GeneratedDocumentMetadataReader
from shared.schemas.identifiers import TenantId
from tool_gateway.errors import ToolCallStatus
from tool_gateway.file_rate_limit import FileDTO
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.quote_files import (
    QuoteFileRecoveryPayload,
    QuoteFileResultSlot,
    fail_file,
    file_call_errors,
    formal_file_postcheck,
    quote_file_generation_key,
    quote_file_generation_parts,
    require_document_binding,
    same_quote_file_snapshot,
)
from tool_gateway.pipeline import PreparedToolCall, ToolCallContext
from tool_gateway.quote_file_ledger import (
    QuoteFileRecoveryPreflight,
    QuoteGenerationLedgerFact,
    QuoteGenerationLedgerReader,
    QuoteRecoveryAudit,
)


class QuoteFileRecoveryPrepared(FileDTO):
    """只保存本次真正检查的三份typed数据，不输出审批/客户内容。"""

    preflight: QuoteFileRecoveryPreflight = Field(repr=False)
    original: QuoteGenerationLedgerFact = Field(repr=False)
    snapshot: QuoteFormalFileSnapshot = Field(repr=False)


def quote_file_recovery_parts(
    snapshot: QuoteFormalFileSnapshot, original: QuoteGenerationLedgerFact
) -> tuple[bytes, ...]:
    """新恢复摘要绑定原生成摘要；技术call键不得成为Store键。"""
    if (
        original.idempotency_key is None
        or original.request_fingerprint is None
        or original.fingerprint_version is None
    ):
        raise ValueError("原生成调用摘要缺失")
    return tuple(
        part.encode("utf-8")
        for part in (
            "quote-file-reconcile-v1",
            snapshot.tenant_id,
            "quotation.file.reconcile",
            original.call_id,
            original.idempotency_key,
            original.request_fingerprint,
            original.fingerprint_version,
            snapshot.quote_content_hash,
            snapshot.customer_content_hash,
            snapshot.approval_run_id,
            snapshot.approval_facts_hash,
            snapshot.template_version,
        )
    )


class QuoteFileRecoveryHandler:
    """依赖必须是独立metadata-only实例，没有生成/下载能力。"""

    def __init__(
        self,
        access: QuoteFileAccessService,
        files: QuoteFileService,
        metadata_only: GeneratedDocumentMetadataReader,
        ledger_reader: QuoteGenerationLedgerReader,
        audit: QuoteRecoveryAudit,
        fingerprints: HmacFingerprintProvider,
        slot: QuoteFileResultSlot,
        *,
        generate_tool_version: str,
        now: Callable[[], datetime],
    ) -> None:
        self._access, self._files, self._metadata = access, files, metadata_only
        self._ledger, self._audit, self._fingerprints, self._slot = (
            ledger_reader,
            audit,
            fingerprints,
            slot,
        )
        self._version, self._now = generate_tool_version, now

    async def _original(
        self, p: QuoteFileRecoveryPreflight, snapshot: QuoteFormalFileSnapshot
    ) -> QuoteGenerationLedgerFact:
        original = await self._ledger.read(p.tenant_id, p.original_generation_call_id)
        if original is None:
            fail_file(self._slot, "original_not_found")
        digest, version = self._fingerprints.fingerprint(
            quote_file_generation_parts(snapshot)
        )
        if (
            original.tenant_id,
            original.call_id,
            original.tool_id,
            original.tool_version,
            original.idempotency_key,
            original.request_fingerprint,
            original.fingerprint_version,
        ) != (
            p.tenant_id,
            p.original_generation_call_id,
            "quotation.file.generate",
            self._version,
            quote_file_generation_key(snapshot),
            digest,
            version,
        ):
            fail_file(self._slot, "original_binding_invalid")
        if (
            original.status != ToolCallStatus.EXECUTING
            or original.provider_ref is not None
        ):
            fail_file(self._slot, "original_state_changed")
        return original

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        """再次正式授权，再读原调用；不把新technical key传给metadata口。"""
        with file_call_errors(self._slot):
            if (
                not isinstance(preflight, QuoteFileRecoveryPreflight)
                or ctx.tool_id != "quotation.file.reconcile"
                or (
                    preflight.tenant_id,
                    preflight.user_id,
                    preflight.actor_id,
                    preflight.quote_id,
                    preflight.original_generation_call_id,
                )
                != (
                    ctx.tenant_id,
                    ctx.user_id,
                    ctx.user_id,
                    ctx.params.get("quote_id"),
                    ctx.params.get("original_generation_call_id"),
                )
                or (
                    any(
                        value is not None
                        for value in (
                            ctx.idempotency_key,
                            ctx.run_id,
                            ctx.approval_ref,
                            ctx.campaign_ref,
                        )
                    )
                    or preflight.recovery_call_id
                    == preflight.original_generation_call_id
                )
            ):
                fail_file(self._slot, "invalid_input")
            snapshot = await self._access.authorize(
                ctx.tenant_id, preflight.quote_id, actor_id=preflight.actor_id
            )
            original = await self._original(preflight, snapshot)
            digest, version = self._fingerprints.fingerprint(
                quote_file_recovery_parts(snapshot, original)
            )
            return PreparedToolCall(
                digest,
                version,
                {},
                QuoteFileRecoveryPrepared(
                    preflight=preflight, original=original, snapshot=snapshot
                ),
            )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str]:
        """请求审计确定退出后才补关联，最后再读原调用状态。"""
        attempted = False
        with file_call_errors(self._slot, writing=lambda: attempted):
            p = prepared.payload
            if (
                not isinstance(p, QuoteFileRecoveryPrepared)
                or p.preflight.tenant_id != tenant_id
            ):
                fail_file(self._slot, "invalid_input")
            preflight = p.preflight
            snapshot = await self._access.authorize(
                tenant_id, preflight.quote_id, actor_id=preflight.actor_id
            )
            if not same_quote_file_snapshot(p.snapshot, snapshot):
                fail_file(self._slot, "original_binding_invalid")
            original = await self._original(preflight, snapshot)
            if original != p.original:
                fail_file(self._slot, "original_binding_invalid")
            meta = await self._metadata.get_meta_by_key(
                tenant_id, quote_file_generation_key(snapshot)
            )
            if meta is None:
                fail_file(self._slot, "metadata_not_found")
            require_document_binding(meta, snapshot)
            await self._audit.append_requested(
                tenant_id,
                preflight=preflight,
                original=original,
                request_fingerprint=prepared.request_fingerprint,
                fingerprint_version=prepared.fingerprint_version,
            )
            attempted = True
            file = await self._files.record_file(
                tenant_id,
                preflight.quote_id,
                meta.artifact_id,
                actor_id=preflight.actor_id,
            )
            require_document_binding(meta, snapshot, file)
            file = await formal_file_postcheck(
                self._access,
                self._files,
                snapshot,
                file.file_id,
                actor_id=preflight.actor_id,
            )
            latest = await self._ledger.read(tenant_id, original.call_id)
            if (
                latest is None
                or latest.status != ToolCallStatus.EXECUTING
                or latest.provider_ref is not None
            ):
                fail_file(self._slot, "original_state_changed")
            if latest != original:
                fail_file(self._slot, "original_binding_invalid")
            self._slot.put(
                QuoteFileRecoveryPayload(
                    file=file,
                    original_generation_call_id=original.call_id,
                    recovery_call_id=preflight.recovery_call_id,
                    original_status_at_check="executing",
                    checked_at=self._now(),
                )
            )
            return {"provider_ref": file.file_id}
