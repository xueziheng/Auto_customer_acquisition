"""报价文件技术应用：只消费真实Gateway状态与一次性结果槽。"""

import re
from collections.abc import Mapping
from typing import Protocol

from domains.quotations.schemas import QuoteFileView, QuoteFormalFileSnapshot
from domains.quotations.service import QuoteFileAccessService, QuoteFileService
from shared.schemas.identifiers import (
    EmployeeId,
    QuoteFileId,
    QuoteId,
    TenantId,
    UserId,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.file_rate_limit import ULID
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.quote_files import (
    FILE_FAILURE_MESSAGES,
    QuoteFileBytesPayload,
    QuoteFileCallFailure,
    QuoteFileFailureCode,
    QuoteFileRecoveryPayload,
    QuoteFileResultSlot,
    formal_file_postcheck,
    quote_file_failure_code,
    quote_file_generation_key,
    quote_file_generation_parts,
)
from tool_gateway.pipeline import ToolCallContext, ToolCallResult
from tool_gateway.quote_file_ledger import (
    QuoteGenerationLedgerFact,
    QuoteGenerationLedgerReader,
)
from tool_gateway.repository import ToolCallId
from workflows.quote_approval.file_schemas import (
    QuoteFileApiError,
    QuoteFileApplicationError,
    QuoteFileRecoveryResult,
)


class QuoteFileGatewayInvoker(Protocol):
    """独立registry装配由B2提供，不在应用内构造网关。"""

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class QuoteFilesApplication:
    """真实员工原样传Gateway；旧ledger不能表示的编号安全拒绝。"""

    def __init__(
        self,
        gateway: QuoteFileGatewayInvoker,
        access: QuoteFileAccessService,
        files: QuoteFileService,
        slot: QuoteFileResultSlot,
        ledger: QuoteGenerationLedgerReader,
        fingerprints: HmacFingerprintProvider,
        *,
        generate_tool_version: str,
    ) -> None:
        self._gateway, self._access, self._files = gateway, access, files
        self._slot, self._ledger, self._fingerprints, self._version = (
            slot,
            ledger,
            fingerprints,
            generate_tool_version,
        )

    def _error(
        self,
        code: QuoteFileFailureCode,
        result: ToolCallResult | None = None,
        *,
        original: ToolCallId | None = None,
        retry: int | None = None,
    ) -> QuoteFileApplicationError:
        return QuoteFileApplicationError(
            QuoteFileApiError(
                code=code,
                message=FILE_FAILURE_MESSAGES[code],
                tool_call_id=ToolCallId(result.tool_call_id)
                if result and result.tool_call_id is not None
                else None,
                original_generation_call_id=original,
                retry_after_seconds=retry,
            )
        )

    async def _bound_ledger(
        self,
        tenant: TenantId,
        snapshot: QuoteFormalFileSnapshot,
        result: ToolCallResult,
    ) -> QuoteGenerationLedgerFact | None:
        """通用claim不核tool_version；插件须在两个短路路径补核。"""
        if result.tool_call_id is None:
            return None
        fact = await self._ledger.read(tenant, ToolCallId(result.tool_call_id))
        digest, version = self._fingerprints.fingerprint(
            quote_file_generation_parts(snapshot)
        )
        if fact is None or (
            fact.tenant_id,
            fact.call_id,
            fact.tool_id,
            fact.tool_version,
            fact.idempotency_key,
            fact.request_fingerprint,
            fact.fingerprint_version,
        ) != (
            tenant,
            result.tool_call_id,
            "quotation.file.generate",
            self._version,
            quote_file_generation_key(snapshot),
            digest,
            version,
        ):
            return None
        return fact

    async def _invoke(
        self,
        tenant: TenantId,
        quote: QuoteId,
        *,
        actor: EmployeeId,
        tool: str,
        params: Mapping[str, str],
        snapshot: QuoteFormalFileSnapshot | None,
    ) -> tuple[ToolCallResult, object | None]:
        ctx = ToolCallContext(
            tenant_id=tenant,
            user_id=UserId(actor),
            tool_id=tool,
            params=params,
            idempotency_key=quote_file_generation_key(snapshot)
            if tool == "quotation.file.generate" and snapshot
            else None,
        )
        result = await self._gateway.invoke(ctx)
        try:
            return await self._consume(
                tenant, quote, actor=actor, tool=tool, result=result
            )
        except QuoteFileApplicationError:
            raise
        except Exception as error:  # noqa: BLE001 -- 已有真实结果不能丢失call ID
            raise self._error(quote_file_failure_code(error), result) from None

    async def _consume(
        self,
        tenant: TenantId,
        quote: QuoteId,
        *,
        actor: EmployeeId,
        tool: str,
        result: ToolCallResult,
    ) -> tuple[ToolCallResult, object | None]:
        if result.tool_id != tool or result.tool_call_id is None:
            raise self._error("storage_inconsistent", result)
        payload = self._slot.take()
        if result.status == ToolCallStatus.SUCCEEDED:
            return result, payload
        if (
            result.status == ToolCallStatus.DUPLICATE
            and tool == "quotation.file.generate"
        ):
            if payload is not None:
                raise self._error("storage_inconsistent", result)
            current = await self._access.authorize(tenant, quote, actor_id=actor)
            fact = await self._bound_ledger(tenant, current, result)
            if fact is None:
                raise self._error("idempotency_conflict", result)
            if fact.status != ToolCallStatus.SUCCEEDED or fact.provider_ref != (
                result.output or {}
            ).get("provider_ref"):
                raise self._error("storage_inconsistent", result)
            return result, None
        if payload is not None and not isinstance(payload, QuoteFileCallFailure):
            raise self._error("storage_inconsistent", result)
        original = None
        categories: dict[ToolErrorCategory | None, QuoteFileFailureCode] = {
            ToolErrorCategory.IDEMPOTENCY_CONFLICT: "idempotency_conflict",
            ToolErrorCategory.IN_PROGRESS: "reconciliation_required",
            ToolErrorCategory.RECONCILIATION_REQUIRED: "reconciliation_required",
            ToolErrorCategory.RATE_LIMITED: "rate_limited",
            ToolErrorCategory.PERMISSION_DENIED: "permission_denied",
            ToolErrorCategory.VALIDATION: "invalid_input",
        }
        code: QuoteFileFailureCode = (
            payload.code
            if isinstance(payload, QuoteFileCallFailure)
            else categories.get(result.error_category, "dependency_unavailable")
        )
        if (
            tool == "quotation.file.generate"
            and payload is None
            and result.error_category
            in {
                ToolErrorCategory.IN_PROGRESS,
                ToolErrorCategory.RECONCILIATION_REQUIRED,
            }
        ):
            current = await self._access.authorize(tenant, quote, actor_id=actor)
            fact = await self._bound_ledger(tenant, current, result)
            if fact is None:
                code = "idempotency_conflict"
            elif fact.status == ToolCallStatus.EXECUTING and fact.provider_ref is None:
                original = fact.call_id
        raise self._error(
            code,
            result,
            original=original,
            retry=payload.retry_after_seconds
            if isinstance(payload, QuoteFileCallFailure)
            else result.retry_after_seconds,
        )

    async def generate(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> QuoteFileView:
        result = None
        try:
            snapshot = await self._access.authorize(
                tenant_id, quote_id, actor_id=actor_id
            )
            result, payload = await self._invoke(
                tenant_id,
                quote_id,
                actor=actor_id,
                tool="quotation.file.generate",
                params={"quote_id": quote_id},
                snapshot=snapshot,
            )
            if payload is not None:
                raise self._error("storage_inconsistent", result)
            reference = (result.output or {}).get("provider_ref")
            if (
                type(reference) is not str
                or re.fullmatch(rf"qfl_{ULID}", reference) is None
            ):
                raise self._error("storage_inconsistent", result)
            return await formal_file_postcheck(
                self._access,
                self._files,
                snapshot,
                QuoteFileId(reference),
                actor_id=actor_id,
            )
        except QuoteFileApplicationError:
            raise
        except Exception as error:  # noqa: BLE001 -- 固定应用错误，不读原文
            raise self._error(quote_file_failure_code(error), result) from None
        finally:
            self._slot.clear()

    async def download(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        file_id: QuoteFileId,
        *,
        actor_id: EmployeeId,
    ) -> tuple[QuoteFileView, bytes]:
        return await self._read(
            tenant_id, quote_id, file_id, actor_id=actor_id, history=False
        )

    async def read_history(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        file_id: QuoteFileId,
        *,
        actor_id: EmployeeId,
    ) -> tuple[QuoteFileView, bytes]:
        return await self._read(
            tenant_id, quote_id, file_id, actor_id=actor_id, history=True
        )

    async def _read(
        self,
        tenant: TenantId,
        quote: QuoteId,
        file: QuoteFileId,
        *,
        actor_id: EmployeeId,
        history: bool,
    ) -> tuple[QuoteFileView, bytes]:
        result = None
        try:
            snapshot = None
            if history:
                await self._access.authorize_history(
                    tenant, quote, file, actor_id=actor_id
                )
            else:
                snapshot = await self._access.authorize(
                    tenant, quote, actor_id=actor_id
                )
            result, payload = await self._invoke(
                tenant,
                quote,
                actor=actor_id,
                tool="quotation.file.history.read"
                if history
                else "quotation.file.read",
                params={"quote_id": quote, "file_id": file},
                snapshot=snapshot,
            )
            if (
                not isinstance(payload, QuoteFileBytesPayload)
                or payload.file.file_id != file
                or payload.file.quote_id != quote
                or (result.output or {}).get("provider_ref") != file
            ):
                raise self._error("storage_inconsistent", result)
            return payload.file, payload.content
        except QuoteFileApplicationError:
            raise
        except Exception as error:  # noqa: BLE001 -- 安全错误边界
            raise self._error(quote_file_failure_code(error), result) from None
        finally:
            self._slot.clear()

    async def reconcile(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        original_generation_call_id: ToolCallId,
        *,
        actor_id: EmployeeId,
    ) -> QuoteFileRecoveryResult:
        result = None
        try:
            snapshot = await self._access.authorize(
                tenant_id, quote_id, actor_id=actor_id
            )
            result, payload = await self._invoke(
                tenant_id,
                quote_id,
                actor=actor_id,
                tool="quotation.file.reconcile",
                params={
                    "quote_id": quote_id,
                    "original_generation_call_id": original_generation_call_id,
                },
                snapshot=snapshot,
            )
            if not isinstance(payload, QuoteFileRecoveryPayload) or (
                payload.recovery_call_id,
                payload.original_generation_call_id,
                payload.file.quote_id,
                payload.file.file_id,
            ) != (
                result.tool_call_id,
                original_generation_call_id,
                quote_id,
                (result.output or {}).get("provider_ref"),
            ):
                raise self._error("storage_inconsistent", result)
            return QuoteFileRecoveryResult(
                outcome="metadata_recovered_original_unresolved",
                file=payload.file,
                original_generation_call_id=payload.original_generation_call_id,
                recovery_call_id=payload.recovery_call_id,
                original_status_at_check=payload.original_status_at_check,
                original_ledger_modified=False,
                checked_at=payload.checked_at,
            )
        except QuoteFileApplicationError:
            raise
        except Exception as error:  # noqa: BLE001 -- 安全错误边界
            raise self._error(quote_file_failure_code(error), result) from None
        finally:
            self._slot.clear()
