"""来源读取工具，业务原文仅在EXECUTING之后进入本调用。"""

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, overload

from pydantic import TypeAdapter

from shared.evidence_read import (
    EvidenceTextParser,
    QuoteEvidenceAccess,
    QuoteEvidenceRawReader,
)
from shared.schemas.evidence_read import (
    AuthorizedEvidenceReference,
    EvidenceParseLimits,
    EvidenceProfile,
    EvidenceReadRequest,
    EvidenceTextResult,
    EvidenceVerifyRequest,
    QuoteEvidenceError,
    QuoteEvidenceErrorCode,
    make_evidence_locator,
    parse_evidence_locator,
    select_evidence_text,
)
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from shared.schemas.quote_facts import fact_identity
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import (
    PreparedToolCall,
    SafeScalar,
    ToolCallContext,
    ToolCallResult,
)

from .quote_evidence_slots import QuoteEvidenceResultSlot

MANIFEST = ToolManifest(
    tool_id="quotation.evidence.read",
    version="v1",
    description="用途授权后有界读取并定位原始资料，原文不进入审计",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("quotation:evidence_read",),
    checks=("tenant", "permission"),
    input_schema=TypeAdapter(EvidenceReadRequest).json_schema(),
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
    redact_fields=(),
)


@overload
def map_evidence_error(value: QuoteEvidenceErrorCode) -> ToolErrorCategory: ...
@overload
def map_evidence_error(value: ToolCallResult) -> QuoteEvidenceErrorCode: ...
def map_evidence_error(
    value: QuoteEvidenceErrorCode | ToolCallResult,
) -> ToolErrorCategory | QuoteEvidenceErrorCode:
    """唯一固定分类映射；没有本调用细码时不猜未知拒绝。"""
    if isinstance(value, ToolCallResult):
        if value.status is ToolCallStatus.DUPLICATE:
            return "gateway_unavailable"
        category = value.error_category
        if category is ToolErrorCategory.VALIDATION:
            return "invalid_input"
        if category is ToolErrorCategory.PERMISSION_DENIED:
            return "permission_denied"
        if category is ToolErrorCategory.PROVIDER_TRANSIENT:
            return "source_unavailable"
        if (
            value.status is ToolCallStatus.REJECTED
            and category is None
            and value.rejected
        ):
            if value.rejected.rule == "tenant:evidence_request":
                return "invalid_input"
            if value.rejected.rule == "tenant:evidence_tenant":
                return "permission_denied"
        return "gateway_unavailable"
    QuoteEvidenceError(value)
    if value == "invalid_input":
        return ToolErrorCategory.VALIDATION
    if value == "permission_denied":
        return ToolErrorCategory.PERMISSION_DENIED
    if value in {"source_unavailable", "gateway_unavailable"}:
        return ToolErrorCategory.PROVIDER_TRANSIENT
    return ToolErrorCategory.PROVIDER_PERMANENT


def read_request(ctx: ToolCallContext) -> EvidenceReadRequest:
    """重验安全输入和员工兼容ID，不触外部依赖。"""
    try:
        fact_identity(ctx.user_id)
        if re.fullmatch(r"tn_[0-7][0-9A-HJKMNP-TV-Z]{25}", ctx.tenant_id) is None:
            raise ValueError
        return TypeAdapter(EvidenceReadRequest).validate_python(
            dict(ctx.params), strict=True
        )
    except (ValueError, TypeError):
        raise QuoteEvidenceError("invalid_input") from None


def _binding(
    reference: AuthorizedEvidenceReference,
    tenant_id: TenantId,
    actor_id: EmployeeId,
    request: EvidenceReadRequest,
) -> None:
    if (
        reference.tenant_id,
        reference.actor_id,
        reference.source_ref,
        reference.scope,
    ) != (tenant_id, actor_id, request.source_ref, request.scope):
        raise QuoteEvidenceError("permission_denied")


def _profile(request: EvidenceReadRequest) -> tuple[EvidenceProfile, int | None]:
    if isinstance(request, EvidenceVerifyRequest):
        selection = parse_evidence_locator(request.locator)
        if selection is None:
            raise QuoteEvidenceError("invalid_input")
        return selection.profile, selection.page
    return request.profile, request.page


@dataclass(frozen=True, repr=False)
class _Payload:
    reference: AuthorizedEvidenceReference
    request: EvidenceReadRequest
    maximum_bytes: int


class QuoteEvidenceGatewayInvoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class QuoteEvidenceReadHandler:
    def __init__(
        self,
        access: QuoteEvidenceAccess,
        raw: QuoteEvidenceRawReader,
        parser: EvidenceTextParser | None,
        slot: QuoteEvidenceResultSlot,
        fingerprints: HmacFingerprintProvider,
        *,
        maximum_raw_bytes: int,
        parser_limits: EvidenceParseLimits,
    ) -> None:
        self._access, self._raw, self._parser = access, raw, parser
        self._slot, self._fingerprints = slot, fingerprints
        self._maximum_raw, self._limits = maximum_raw_bytes, parser_limits
        try:
            if type(maximum_raw_bytes) is not int or maximum_raw_bytes <= 0:
                raise ValueError
            self._limits = EvidenceParseLimits.model_validate(
                parser_limits.model_dump()
            )
        except (ValueError, AttributeError):
            raise QuoteEvidenceError("invalid_input") from None

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        """只做当前metadata授权、能力快照与HMAC，不probe/读原件。"""
        try:
            request = read_request(ctx)
            actor_id = EmployeeId(str(ctx.user_id))
            if not isinstance(preflight, AuthorizedEvidenceReference):
                raise QuoteEvidenceError("permission_denied")
            _binding(preflight, ctx.tenant_id, actor_id, request)
            current = await self._access.authorize(
                ctx.tenant_id,
                request.source_ref,
                actor_id=actor_id,
                scope=request.scope,
            )
            if current != preflight:
                raise QuoteEvidenceError("permission_denied")
            root = isinstance(request, EvidenceVerifyRequest) and request.locator == "$"
            maximum = (
                self._maximum_raw
                if root
                else min(self._maximum_raw, self._limits.maximum_input_bytes)
            )
            if current.raw.size_bytes > maximum:
                raise QuoteEvidenceError("source_limit_exceeded")
            if not root:
                try:
                    capability = (
                        self._parser.capability() if self._parser is not None else None
                    )
                except Exception:  # noqa: BLE001 - 能力错误安全关闭
                    raise QuoteEvidenceError("parse_unavailable") from None
                if capability is None or capability.status != "available":
                    raise QuoteEvidenceError("parse_unavailable")
            encoded = json.dumps(
                {
                    "version": "quotation-evidence-v1",
                    "tenant_id": ctx.tenant_id,
                    "actor_id": ctx.user_id,
                    "request": request.model_dump(mode="json"),
                    "artifact_id": current.raw.artifact_id,
                    "raw_hash": current.raw.content_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
            fingerprint, version = self._fingerprints.fingerprint((encoded,))
            profile = None if root else _profile(request)[0]
            return PreparedToolCall(
                fingerprint,
                version,
                {
                    "source_ref": request.source_ref,
                    "operation": request.operation,
                    "purpose": request.scope.purpose,
                    "profile": profile,
                },
                _Payload(current, request, maximum),
            )
        except QuoteEvidenceError as error:
            self._slot.put_failure(error.code)
            raise ToolGatewayError(map_evidence_error(error.code)) from None
        except Exception:  # noqa: BLE001 - 只传固定失败码
            self._slot.put_failure("source_unavailable")
            raise ToolGatewayError(map_evidence_error("source_unavailable")) from None

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, SafeScalar]:
        """Gateway已提交EXECUTING后才读bytes，交付前再次当前授权。"""
        try:
            payload = prepared.payload
            if (
                not isinstance(payload, _Payload)
                or payload.reference.tenant_id != tenant_id
            ):
                raise QuoteEvidenceError("invalid_input")
            reference, request = payload.reference, payload.request
            content = await self._raw.read(
                tenant_id,
                reference.raw.artifact_id,
                maximum_bytes=payload.maximum_bytes,
            )
            if (
                content.meta != reference.raw
                or type(content.content) is not bytes
                or len(content.content) != reference.raw.size_bytes
                or hashlib.sha256(content.content).hexdigest()
                != reference.raw.content_hash
            ):
                raise QuoteEvidenceError("source_integrity_failed")
            root = isinstance(request, EvidenceVerifyRequest) and request.locator == "$"
            profile = page = text = text_hash = selection = excerpt = locator = None
            if root:
                locator = "$"
            else:
                expected = (
                    parse_evidence_locator(request.locator)
                    if isinstance(request, EvidenceVerifyRequest)
                    else None
                )
                profile, page = _profile(request)
                if self._parser is None:
                    raise QuoteEvidenceError("parse_unavailable")
                try:
                    parsed = await self._parser.parse(
                        content.content, profile=profile, page=page
                    )
                except QuoteEvidenceError:
                    raise
                except Exception:  # noqa: BLE001 - 未分类解析错误不得携原文
                    raise QuoteEvidenceError("parse_unavailable") from None
                if parsed.profile != profile or parsed.page != page:
                    raise QuoteEvidenceError("parse_unavailable")
                text = parsed.text
                if len(text.encode("utf-8")) > self._limits.maximum_text_bytes:
                    raise QuoteEvidenceError("parse_limit_exceeded")
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if request.operation == "locate":
                    if (request.expected_raw_hash, request.expected_text_hash) != (
                        content.meta.content_hash,
                        text_hash,
                    ):
                        raise QuoteEvidenceError("locator_mismatch")
                    selection, excerpt = select_evidence_text(
                        parsed,
                        request.start,
                        request.end,
                        maximum_excerpt_bytes=self._limits.maximum_excerpt_bytes,
                    )
                elif expected is not None:
                    selection, excerpt = select_evidence_text(
                        parsed,
                        expected.start,
                        expected.end,
                        maximum_excerpt_bytes=self._limits.maximum_excerpt_bytes,
                    )
                    if selection != expected:
                        raise QuoteEvidenceError("locator_mismatch")
                if selection is not None:
                    locator = make_evidence_locator(selection)
            current = await self._access.authorize(
                tenant_id,
                request.source_ref,
                actor_id=reference.actor_id,
                scope=request.scope,
            )
            if current != reference:
                raise QuoteEvidenceError("permission_denied")
            result = EvidenceTextResult(
                reference=reference,
                profile=profile,
                page=page,
                text=text,
                text_hash=text_hash,
                selection=selection,
                excerpt=excerpt,
                locator=locator,
            )
            return {"provider_ref": self._slot.put(result)}
        except QuoteEvidenceError as error:
            self._slot.put_failure(error.code)
            raise ToolGatewayError(map_evidence_error(error.code)) from None
        except Exception:  # noqa: BLE001 - 原文/协议/连接异常只给固定码
            self._slot.put_failure("source_unavailable")
            raise ToolGatewayError(map_evidence_error("source_unavailable")) from None


class ToolGatewayQuoteEvidenceReader:
    def __init__(
        self,
        gateway: QuoteEvidenceGatewayInvoker,
        slot: QuoteEvidenceResultSlot,
        access: QuoteEvidenceAccess,
    ) -> None:
        self._gateway, self._slot, self._access = gateway, slot, access

    async def read(
        self, tenant_id: TenantId, request: EvidenceReadRequest, *, actor_id: EmployeeId
    ) -> EvidenceTextResult:
        """只有真实SUCCEEDED才领取原文，任何失败/取消均清理本调用槽。"""
        if not self._slot.is_empty:
            raise QuoteEvidenceError("gateway_unavailable")
        try:
            try:
                ctx = ToolCallContext(
                    tenant_id,
                    UserId(str(actor_id)),
                    MANIFEST.tool_id,
                    request.model_dump(),
                    run_id=None,
                )
                # 不允许str(bool)绕过员工兼容ID规则。
                fact_identity(actor_id)
                read_request(ctx)
            except (ValueError, AttributeError):
                raise QuoteEvidenceError("invalid_input") from None
            try:
                response = await self._gateway.invoke(ctx)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 审计失败不得领取成功内容
                raise QuoteEvidenceError("gateway_unavailable") from None
            if not isinstance(response, ToolCallResult):
                raise QuoteEvidenceError("gateway_unavailable")
            if response.status is not ToolCallStatus.SUCCEEDED:
                code = self._slot.take_failure()
                raise QuoteEvidenceError(code or map_evidence_error(response))
            handle = response.output.get("provider_ref") if response.output else None
            if type(handle) is not str:
                raise QuoteEvidenceError("gateway_unavailable")
            result = self._slot.take(handle)
            _binding(result.reference, tenant_id, actor_id, request)
            try:
                current = await self._access.authorize(
                    tenant_id,
                    request.source_ref,
                    actor_id=actor_id,
                    scope=request.scope,
                )
            except QuoteEvidenceError:
                raise
            except Exception:  # noqa: BLE001 - 末次授权失败不泄露依赖错误
                raise QuoteEvidenceError("source_unavailable") from None
            if current != result.reference:
                raise QuoteEvidenceError("permission_denied")
            return result
        finally:
            self._slot.discard_all()
