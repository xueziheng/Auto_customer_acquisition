"""contact.enrich Gateway 插件与同调用栈 typed 结果交接。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from connectors.contact_enrichment.client import ContactEnrichmentResult
from connectors.hunter.client import (
    HunterConnector,
    HunterConnectorError,
    HunterSecretResolver,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    UserId,
)
from tool_gateway.checks.contact_provider import ContactDiscoveryPreflight
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
    tool_id="contact.enrich",
    version="v1",
    description="按已批准的企业需求事实补全带来源的联系人候选",
    risk_level=RiskLevel.MEDIUM,
    cost_class=CostClass.MEDIUM,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("contact:enrich",),
    checks=(
        "tenant",
        "permission",
        "playbook",
        "country_policy",
        "suppression",
        "rate_limit",
    ),
    input_schema={
        "type": "object",
        "required": ("hypothesis_id", "account_id", "role_hints"),
        "properties": {
            "hypothesis_id": {"type": "string"},
            "account_id": {"type": "string"},
            "role_hints": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
        },
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

_HYPOTHESIS_RE = re.compile(r"hyp_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ACCOUNT_RE = re.compile(r"acc_[0-7][0-9A-HJKMNP-TV-Z]{25}")


@runtime_checkable
class _ProviderContactEnricher(Protocol):
    async def find_contacts(
        self,
        tenant_id: TenantId,
        company_domain: str,
        role_hints: tuple[str, ...],
    ) -> ContactEnrichmentResult: ...


@runtime_checkable
class _ToolGatewayInvoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class HunterProviderContactEnricher:
    """全部 Gateway checks 通过后才创建 connector 并解析凭证。"""

    def __init__(
        self,
        connector_factory: Callable[[TenantId], HunterConnector],
        secret_resolver: HunterSecretResolver,
    ) -> None:
        if not callable(connector_factory) or not isinstance(
            secret_resolver, HunterSecretResolver
        ):
            raise ValidationError("Hunter 联系人 reader 依赖无效")
        self._connector_factory = connector_factory
        self._secret_resolver = secret_resolver

    async def find_contacts(
        self,
        tenant_id: TenantId,
        company_domain: str,
        role_hints: tuple[str, ...],
    ) -> ContactEnrichmentResult:
        connector = self._connector_factory(tenant_id)
        if not isinstance(connector, HunterConnector):
            raise ValidationError("Hunter 联系人 connector 无效")
        await connector.configure(self._secret_resolver)
        return await connector.find_contacts(company_domain, role_hints)


_HunterProviderContactEnricher = HunterProviderContactEnricher


@dataclass(frozen=True, repr=False)
class _ContactEnrichmentPayload:
    tenant_id: TenantId
    hypothesis_id: NeedHypothesisId
    account_id: ProspectAccountId
    company_domain: str = field(repr=False)
    role_hints: tuple[str, ...] = field(repr=False)


class ContactEnrichmentHandler:
    """准备安全投影，执行后只返回 task-local `ceb_` handle。"""

    def __init__(
        self,
        reader: _ProviderContactEnricher,
        slot: ContextLocalSingleResultSlot[ContactEnrichmentResult],
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        if (
            not isinstance(reader, _ProviderContactEnricher)
            or not isinstance(slot, ContextLocalSingleResultSlot)
            or not isinstance(fingerprints, HmacFingerprintProvider)
        ):
            raise ValidationError("contact enrichment handler 依赖无效")
        self._reader = reader
        self._slot = slot
        self._fingerprints = fingerprints

    async def prepare(
        self,
        ctx: ToolCallContext,
        preflight: object | None,
    ) -> PreparedToolCall:
        if set(ctx.params) != {"hypothesis_id", "account_id", "role_hints"}:
            raise ValidationError("contact enrichment params 无效")
        hypothesis_value = ctx.params.get("hypothesis_id")
        account_value = ctx.params.get("account_id")
        raw_hints = ctx.params.get("role_hints")
        if (
            not isinstance(hypothesis_value, str)
            or _HYPOTHESIS_RE.fullmatch(hypothesis_value) is None
            or not isinstance(account_value, str)
            or _ACCOUNT_RE.fullmatch(account_value) is None
            or type(raw_hints) is not tuple
            or len(raw_hints) > 10
        ):
            raise ValidationError("contact enrichment params 无效")
        hints = _canonical_hints(raw_hints)
        hypothesis_id = NeedHypothesisId(hypothesis_value)
        account_id = ProspectAccountId(account_value)
        if (
            not isinstance(preflight, ContactDiscoveryPreflight)
            or preflight.tenant_id != ctx.tenant_id
            or preflight.hypothesis_id != hypothesis_id
            or preflight.account_id != account_id
        ):
            raise ValidationError("contact enrichment preflight 无效")
        fingerprint, version = self._fingerprints.fingerprint(
            (
                str(ctx.tenant_id).encode(),
                hypothesis_value.encode(),
                account_value.encode(),
                preflight.website_domain.encode(),
                *(hint.encode() for hint in hints),
            )
        )
        return PreparedToolCall(
            fingerprint,
            version,
            {
                "hypothesis_id": hypothesis_value,
                "account_id": account_value,
                "role_hint_count": len(hints),
            },
            _ContactEnrichmentPayload(
                ctx.tenant_id,
                hypothesis_id,
                account_id,
                preflight.website_domain,
                hints,
            ),
        )

    async def execute(
        self,
        tenant_id: TenantId,
        prepared: PreparedToolCall,
    ) -> Mapping[str, SafeScalar]:
        payload = prepared.payload
        if (
            not isinstance(payload, _ContactEnrichmentPayload)
            or payload.tenant_id != tenant_id
        ):
            raise ValidationError("contact enrichment payload 无效")
        mapped_error: ToolGatewayError | None = None
        try:
            result = await self._reader.find_contacts(
                tenant_id,
                payload.company_domain,
                payload.role_hints,
            )
        except HunterConnectorError as error:
            mapped_error = map_contact_provider_error(error)
            result = None
        except BaseException:
            self._slot.discard_all()
            raise
        if mapped_error is not None:
            self._slot.discard_all()
            raise mapped_error
        if not isinstance(result, ContactEnrichmentResult):
            self._slot.discard_all()
            raise ValidationError("contact enrichment reader result 无效")
        try:
            handle = self._slot.put(result)
        except BaseException:
            self._slot.discard_all()
            raise
        return {"provider_ref": handle}


class ToolGatewayContactEnricher:
    """workflow-facing 可信 adapter；Gateway 成功后同栈领取一次结果。"""

    def __init__(
        self,
        gateway: _ToolGatewayInvoker,
        slot: ContextLocalSingleResultSlot[ContactEnrichmentResult],
        user_id: UserId,
    ) -> None:
        if (
            not isinstance(gateway, _ToolGatewayInvoker)
            or not isinstance(slot, ContextLocalSingleResultSlot)
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ValidationError("contact enrichment adapter 依赖无效")
        self._gateway = gateway
        self._slot = slot
        self._user_id = user_id

    async def find_contacts(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
        role_hints: tuple[str, ...],
    ) -> ContactEnrichmentResult:
        try:
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id,
                    self._user_id,
                    MANIFEST.tool_id,
                    {
                        "hypothesis_id": str(hypothesis_id),
                        "account_id": str(account_id),
                        "role_hints": role_hints,
                    },
                )
            )
            if not isinstance(result, ToolCallResult):
                raise ValidationError("contact enrichment tool result 无效")
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ToolGatewayError(
                    result.error_category or ToolErrorCategory.UNEXPECTED,
                    retry_after_seconds=result.retry_after_seconds,
                )
            handle = None if result.output is None else result.output.get("provider_ref")
            if not isinstance(handle, str):
                raise ValidationError("contact enrichment tool result 无效")
            taken = self._slot.take(handle)
            if not isinstance(taken, ContactEnrichmentResult):
                raise ValidationError("contact enrichment slot result 无效")
            return taken
        except BaseException:
            self._slot.discard_all()
            raise


def _canonical_hints(raw_hints: tuple[object, ...]) -> tuple[str, ...]:
    canonical: set[str] = set()
    for value in raw_hints:
        if not isinstance(value, str):
            raise ValidationError("contact enrichment params 无效")
        normalized = " ".join(value.split()).casefold()
        if not normalized or len(normalized.encode()) > 200:
            raise ValidationError("contact enrichment params 无效")
        canonical.add(normalized)
    return tuple(sorted(canonical))


__all__ = (
    "MANIFEST",
    "ContactEnrichmentHandler",
    "HunterProviderContactEnricher",
    "ToolGatewayContactEnricher",
)
