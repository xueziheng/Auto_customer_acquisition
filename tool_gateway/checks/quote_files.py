"""文件工具当前身份、用途授权及claim后的持久预留。"""

import re
from dataclasses import replace
from typing import cast

from domains.quotations.service import QuotationActorReader, QuoteFileAccessService
from shared.schemas.identifiers import EmployeeId, QuoteFileId, UserId
from tool_gateway.errors import ToolErrorCategory
from tool_gateway.file_rate_limit import (
    ULID,
    QuoteFileGenerationRateLimiter,
    QuoteFileRateError,
    QuoteFileRateRequest,
)
from tool_gateway.handlers.quote_files import (
    FileTool,
    QuoteFilePreflight,
    QuoteFilePrepared,
    QuoteFileResultSlot,
    fail_file,
    file_call_errors,
    require_file_preflight,
    same_quote_file_snapshot,
)
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState
from tool_gateway.quote_file_ledger import QuoteFileRecoveryPreflight
from tool_gateway.repository import ToolCallId


class QuoteFileTenantCheck:
    """固定参数及无外部授权字段；真实tenant归属另由域检查。"""

    name = "tenant"

    def __init__(self, slot: QuoteFileResultSlot) -> None:
        self._slot = slot

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        with file_call_errors(self._slot):
            expected = {
                "quotation.file.generate": {"quote_id": "quo"},
                "quotation.file.read": {"quote_id": "quo", "file_id": "qfl"},
                "quotation.file.history.read": {"quote_id": "quo", "file_id": "qfl"},
                "quotation.file.reconcile": {
                    "quote_id": "quo",
                    "original_generation_call_id": "tcl",
                },
            }.get(ctx.tool_id)
            if (
                expected is None
                or ctx.tool_id != state.manifest.tool_id
                or set(ctx.params) != set(expected)
                or re.fullmatch(rf"tn_{ULID}", ctx.tenant_id) is None
            ):
                fail_file(self._slot, "invalid_input")
            if any(
                value is not None
                for value in (ctx.run_id, ctx.approval_ref, ctx.campaign_ref)
            ):
                fail_file(self._slot, "invalid_input")
            for name, prefix in expected.items():
                value = ctx.params[name]
                if (
                    type(value) is not str
                    or re.fullmatch(rf"{prefix}_{ULID}", value) is None
                ):
                    fail_file(self._slot, "invalid_input")
            if (
                ctx.tool_id != "quotation.file.generate"
                and ctx.idempotency_key is not None
            ):
                fail_file(self._slot, "invalid_input")
        return None


class QuoteFilePermissionCheck:
    """本次真实员工身份从ctx取得，不写共享可变actor。"""

    name = "permission"

    def __init__(
        self,
        actors: QuotationActorReader,
        access: QuoteFileAccessService,
        slot: QuoteFileResultSlot,
    ) -> None:
        self._actors, self._access, self._slot = actors, access, slot

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        with file_call_errors(self._slot):
            actor_id = EmployeeId(ctx.user_id)
            actor = await self._actors.read_current(ctx.tenant_id, actor_id)
            if actor is None or (
                actor.tenant_id,
                actor.employee_id,
                actor.is_active,
            ) != (ctx.tenant_id, actor_id, True):
                fail_file(self._slot, "permission_denied")
            quote_id, file_id = ctx.params["quote_id"], ctx.params.get("file_id")
            snapshot, historical = None, None
            if ctx.tool_id == "quotation.file.history.read":
                if file_id is None:
                    fail_file(self._slot, "invalid_input")
                historical = await self._access.authorize_history(
                    ctx.tenant_id, quote_id, QuoteFileId(file_id), actor_id=actor_id
                )
            else:
                snapshot = await self._access.authorize(
                    ctx.tenant_id, quote_id, actor_id=actor_id
                )
            if ctx.tool_id == "quotation.file.reconcile":
                state.preflight = QuoteFileRecoveryPreflight(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    actor_id=actor_id,
                    quote_id=quote_id,
                    original_generation_call_id=ctx.params[
                        "original_generation_call_id"
                    ],
                    recovery_call_id=ToolCallId(state.tool_call_id),
                    recovery_tool_version=state.manifest.version,
                )
            else:
                state.preflight = QuoteFilePreflight(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    actor_id=actor_id,
                    tool_id=cast(FileTool, ctx.tool_id),
                    quote_id=quote_id,
                    file_id=file_id,
                    snapshot=snapshot,
                    history_file=historical,
                )
        return None


class QuoteFileApprovalCheck:
    """正式用途每次读完整当前批准，不用requires_approval布尔代替。"""

    name = "approval"

    def __init__(
        self, access: QuoteFileAccessService, slot: QuoteFileResultSlot
    ) -> None:
        self._access, self._slot = access, slot

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        with file_call_errors(self._slot):
            p = state.preflight
            if ctx.tool_id == "quotation.file.reconcile":
                if not isinstance(p, QuoteFileRecoveryPreflight) or (
                    p.tenant_id,
                    p.user_id,
                    p.actor_id,
                    p.quote_id,
                    p.original_generation_call_id,
                    p.recovery_call_id,
                    p.recovery_tool_version,
                ) != (
                    ctx.tenant_id,
                    ctx.user_id,
                    ctx.user_id,
                    ctx.params["quote_id"],
                    ctx.params["original_generation_call_id"],
                    state.tool_call_id,
                    state.manifest.version,
                ):
                    fail_file(self._slot, "invalid_input")
                await self._access.authorize(
                    ctx.tenant_id, p.quote_id, actor_id=p.actor_id
                )
            else:
                if ctx.tool_id not in {
                    "quotation.file.generate",
                    "quotation.file.read",
                }:
                    fail_file(self._slot, "invalid_input")
                p = require_file_preflight(ctx, p, cast(FileTool, ctx.tool_id))
                snapshot = await self._access.authorize(
                    ctx.tenant_id, p.quote_id, actor_id=p.actor_id
                )
                if p.snapshot is None or not same_quote_file_snapshot(
                    p.snapshot, snapshot
                ):
                    fail_file(self._slot, "idempotency_conflict")
                state.preflight = p.model_copy(update={"snapshot": snapshot})
        return None


class QuoteFileRateLimitCheck:
    """claim已成功后才能独立预留；限速失败不能将CLAIMED写成REJECTED。"""

    name = "rate_limit"

    def __init__(
        self, limiter: QuoteFileGenerationRateLimiter, slot: QuoteFileResultSlot
    ) -> None:
        self._limiter, self._slot = limiter, slot

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        with file_call_errors(self._slot):
            prepared = state.prepared
            if (
                ctx.tool_id != "quotation.file.generate"
                or state.manifest.tool_id != ctx.tool_id
                or prepared is None
                or ctx.idempotency_key is None
            ):
                fail_file(self._slot, "invalid_input")
            p = prepared.payload
            if (
                not isinstance(p, QuoteFilePrepared)
                or p.rate_claim is not None
                or (p.tenant_id, p.actor_id, p.tool_id, p.quote_id)
                != (ctx.tenant_id, ctx.user_id, ctx.tool_id, ctx.params["quote_id"])
            ):
                fail_file(self._slot, "invalid_input")
            request = QuoteFileRateRequest(
                canonical_call_id=ToolCallId(state.tool_call_id),
                tool_version=state.manifest.version,
                idempotency_key=ctx.idempotency_key,
                request_fingerprint=prepared.request_fingerprint,
                fingerprint_version=prepared.fingerprint_version,
                actor_id=UserId(ctx.user_id),
            )
            try:
                decision = await self._limiter.reserve(ctx.tenant_id, request)
            except QuoteFileRateError as error:
                if error.code == "claim_invalid":
                    fail_file(self._slot, "reconciliation_required")
                if error.code == "invalid_input":
                    fail_file(self._slot, "invalid_input")
                fail_file(
                    self._slot,
                    "dependency_unavailable",
                    category=ToolErrorCategory.PROVIDER_TRANSIENT,
                )
            if decision.outcome == "limited":
                fail_file(self._slot, "rate_limited", decision.retry_after_seconds)
            state.prepared = replace(
                prepared, payload=p.model_copy(update={"rate_claim": request})
            )
        return None
