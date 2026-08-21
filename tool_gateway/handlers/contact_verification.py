"""contact.verify Gateway 插件与同调用栈 typed 结果交接。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from connectors.hunter.client import (
    HunterConnector,
    HunterConnectorError,
    HunterSecretResolver,
)
from domains.prospecting.schemas import ContactPointKind, VerificationStatus
from shared.errors import ValidationError
from shared.schemas.identifiers import ContactPointId, TenantId, UserId
from tool_gateway.checks.contact_provider import ContactVerificationPreflight
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.contact_provider_errors import map_contact_provider_error
from tool_gateway.handlers.single_result_slot import ContextLocalSingleResultSlot
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

MANIFEST = ToolManifest(
    tool_id="contact.verify",
    version="v1",
    description="验证已授权邮箱联系点的当前可达性",
    risk_level=RiskLevel.MEDIUM,
    cost_class=CostClass.LOW,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("contact:verify",),
    checks=("tenant", "permission", "suppression", "rate_limit"),
    input_schema={
        "type": "object",
        "required": ("contact_point_id",),
        "properties": {"contact_point_id": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
    redact_fields=(),
)

_CONTACT_POINT_RE = re.compile(r"cp_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_CACHE_TTL = timedelta(days=30)
_OUTCOMES = {
    VerificationStatus.VERIFIED: EmailVerificationOutcome.VERIFIED,
    VerificationStatus.INVALID: EmailVerificationOutcome.INVALID,
    VerificationStatus.RISKY: EmailVerificationOutcome.RISKY,
    VerificationStatus.UNVERIFIED: EmailVerificationOutcome.UNVERIFIED,
}


@runtime_checkable
class _ProviderContactVerifier(Protocol):
    async def verify(
        self,
        tenant_id: TenantId,
        email: str,
    ) -> EmailVerificationResult: ...


@runtime_checkable
class _ToolGatewayInvoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class HunterProviderContactVerifier:
    """全部 Gateway checks 通过后才创建 connector 并解析凭证。"""

    def __init__(
        self,
        connector_factory: Callable[[TenantId], HunterConnector],
        secret_resolver: HunterSecretResolver,
    ) -> None:
        if not callable(connector_factory) or not isinstance(
            secret_resolver, HunterSecretResolver
        ):
            raise ValidationError("Hunter 邮箱验证 reader 依赖无效")
        self._connector_factory = connector_factory
        self._secret_resolver = secret_resolver

    async def verify(
        self,
        tenant_id: TenantId,
        email: str,
    ) -> EmailVerificationResult:
        connector = self._connector_factory(tenant_id)
        if not isinstance(connector, HunterConnector):
            raise ValidationError("Hunter 邮箱验证 connector 无效")
        await connector.configure(self._secret_resolver)
        return await connector.verify(email)


_HunterProviderContactVerifier = HunterProviderContactVerifier


@dataclass(frozen=True, repr=False)
class _ContactVerificationPayload:
    tenant_id: TenantId
    contact_point_id: ContactPointId
    email: str | None = field(repr=False)
    cached_result: EmailVerificationResult | None = field(repr=False)


class ContactVerificationHandler:
    """准备安全投影，优先复用 30 天缓存并返回 task-local handle。"""

    def __init__(
        self,
        reader: _ProviderContactVerifier,
        slot: ContextLocalSingleResultSlot[EmailVerificationResult],
        fingerprints: HmacFingerprintProvider,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            not isinstance(reader, _ProviderContactVerifier)
            or not isinstance(slot, ContextLocalSingleResultSlot)
            or not isinstance(fingerprints, HmacFingerprintProvider)
            or not callable(now)
        ):
            raise ValidationError("contact verification handler 依赖无效")
        self._reader = reader
        self._slot = slot
        self._fingerprints = fingerprints
        self._now = now

    async def prepare(
        self,
        ctx: ToolCallContext,
        preflight: object | None,
    ) -> PreparedToolCall:
        if set(ctx.params) != {"contact_point_id"}:
            raise ValidationError("contact verification params 无效")
        point_value = ctx.params.get("contact_point_id")
        if (
            not isinstance(point_value, str)
            or _CONTACT_POINT_RE.fullmatch(point_value) is None
        ):
            raise ValidationError("contact verification params 无效")
        point_id = ContactPointId(point_value)
        if (
            not isinstance(preflight, ContactVerificationPreflight)
            or preflight.tenant_id != ctx.tenant_id
            or preflight.contact_point.tenant_id != ctx.tenant_id
            or preflight.contact_point.contact_point_id != point_id
            or preflight.contact_point.kind is not ContactPointKind.EMAIL
            or not isinstance(preflight.contact_point.verification, VerificationStatus)
        ):
            raise ValidationError("contact verification preflight 无效")

        now = self._now()
        if not _utc(now):
            raise ValidationError("contact verification clock 无效")
        point = preflight.contact_point
        checked_at = point.verification_checked_at
        cache_hit = preflight.cache_valid
        cached_result = None
        if cache_hit:
            provider = point.verification_provider
            if (
                checked_at is None
                or not _utc(checked_at)
                or not checked_at <= now < checked_at + _CACHE_TTL
                or not isinstance(provider, str)
                or not provider
            ):
                raise ValidationError("contact verification cache 无效")
            cached_result = EmailVerificationResult(
                _OUTCOMES[point.verification],
                provider,
                checked_at,
                VerificationCostNote.CACHE_HIT,
            )

        fingerprint, version = self._fingerprints.fingerprint(
            (str(ctx.tenant_id).encode(), point_value.encode())
        )
        return PreparedToolCall(
            fingerprint,
            version,
            {"contact_point_id": point_value, "cache_hit": cache_hit},
            _ContactVerificationPayload(
                ctx.tenant_id,
                point_id,
                None if cache_hit else point.value,
                cached_result,
            ),
        )

    async def execute(
        self,
        tenant_id: TenantId,
        prepared: PreparedToolCall,
    ) -> Mapping[str, SafeScalar]:
        payload = prepared.payload
        if (
            not isinstance(payload, _ContactVerificationPayload)
            or payload.tenant_id != tenant_id
        ):
            raise ValidationError("contact verification payload 无效")
        mapped_error: ToolGatewayError | None = None
        try:
            result = payload.cached_result
            if result is None:
                if not isinstance(payload.email, str):
                    raise ValidationError("contact verification payload 无效")
                result = await self._reader.verify(tenant_id, payload.email)
        except HunterConnectorError as error:
            mapped_error = map_contact_provider_error(error)
            result = None
        except BaseException:
            self._slot.discard_all()
            raise
        if mapped_error is not None:
            self._slot.discard_all()
            raise mapped_error
        if not isinstance(result, EmailVerificationResult):
            self._slot.discard_all()
            raise ValidationError("contact verification reader result 无效")
        try:
            handle = self._slot.put(result)
        except BaseException:
            self._slot.discard_all()
            raise
        return {"provider_ref": handle}


class ToolGatewayContactVerifier:
    """workflow-facing 可信 adapter；Gateway 成功后同栈领取一次结果。"""

    def __init__(
        self,
        gateway: _ToolGatewayInvoker,
        slot: ContextLocalSingleResultSlot[EmailVerificationResult],
        user_id: UserId,
    ) -> None:
        if (
            not isinstance(gateway, _ToolGatewayInvoker)
            or not isinstance(slot, ContextLocalSingleResultSlot)
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ValidationError("contact verification adapter 依赖无效")
        self._gateway = gateway
        self._slot = slot
        self._user_id = user_id

    async def verify(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
    ) -> EmailVerificationResult:
        try:
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id,
                    self._user_id,
                    MANIFEST.tool_id,
                    {"contact_point_id": str(contact_point_id)},
                )
            )
            if not isinstance(result, ToolCallResult):
                raise ValidationError("contact verification tool result 无效")
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ToolGatewayError(
                    result.error_category or ToolErrorCategory.UNEXPECTED,
                    retry_after_seconds=result.retry_after_seconds,
                )
            handle = (
                None if result.output is None else result.output.get("provider_ref")
            )
            if not isinstance(handle, str):
                raise ValidationError("contact verification tool result 无效")
            taken = self._slot.take(handle)
            if not isinstance(taken, EmailVerificationResult):
                raise ValidationError("contact verification slot result 无效")
            return taken
        except BaseException:
            self._slot.discard_all()
            raise


def _utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


__all__ = (
    "MANIFEST",
    "ContactVerificationHandler",
    "HunterProviderContactVerifier",
    "ToolGatewayContactVerifier",
)
