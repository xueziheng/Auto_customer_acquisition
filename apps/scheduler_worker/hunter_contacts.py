"""Hunter 联系人插件的生产 Tool Gateway 组装。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.contact_enrichment.client import ContactEnrichmentResult
from connectors.email_verification.client import EmailVerificationResult
from connectors.hunter.client import (
    HunterConnector,
    HunterSecretResolver,
)
from connectors.hunter.transport import HunterHttpTransport
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import SuppressionTarget
from domains.outreach.service import OutreachService
from domains.prospecting.service import ProspectingService
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.checks.contact_provider import (
    ContactCountryPolicyCheck,
    ContactCountryPolicyReader,
    ContactDiscoveryPolicyReader,
    ContactEnrichmentPlaybookCheck,
    ContactProviderRateLimitCheck,
    ContactProviderSuppressionCheck,
    ContactResourceTenantCheck,
    InMemoryHunterQuotaGuard,
    ProviderQuotaGuard,
)
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.contact_enrichment import (
    MANIFEST as CONTACT_ENRICH_MANIFEST,
)
from tool_gateway.handlers.contact_enrichment import (
    ContactEnrichmentHandler,
    HunterProviderContactEnricher,
    ToolGatewayContactEnricher,
)
from tool_gateway.handlers.contact_verification import (
    MANIFEST as CONTACT_VERIFY_MANIFEST,
)
from tool_gateway.handlers.contact_verification import (
    ContactVerificationHandler,
    HunterProviderContactVerifier,
    ToolGatewayContactVerifier,
)
from tool_gateway.handlers.single_result_slot import ContextLocalSingleResultSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckStage,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import (
    ToolGatewayUnitOfWork,
    ToolGatewayUnitOfWorkFactory,
)
from workflows.account_discovery.ports import ContactEnricher, ContactVerifier


class _BoundHunterSecretResolver:
    """把 Hunter 固定逻辑引用映射到部署配置的环境密钥引用。"""

    def __init__(self, resolver: HunterSecretResolver, configured_ref: str) -> None:
        if (
            not isinstance(resolver, HunterSecretResolver)
            or not isinstance(configured_ref, str)
            or not configured_ref
            or configured_ref != configured_ref.strip()
        ):
            raise ValidationError("Hunter 密钥引用配置无效")
        self._resolver = resolver
        self._configured_ref = configured_ref

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "HUNTER_API_KEY_REF":
            raise ValidationError("Hunter 凭证引用无效")
        return self._resolver.resolve(self._configured_ref)


class _OutreachContactSuppressionReader:
    def __init__(self, outreach: OutreachService) -> None:
        if not callable(getattr(outreach, "is_suppressed", None)):
            raise ValidationError("Hunter 抑制依赖无效")
        self._outreach = outreach

    async def is_suppressed(
        self, tenant_id: TenantId, target: SuppressionTarget
    ) -> bool:
        actor = OutreachActor(
            "system:scheduler-hunter",
            OutreachScope(
                level=OutreachScopeLevel.SYSTEM,
                allowed_suppression_targets=frozenset({target.canonical_id}),
            ),
            "system",
        )
        return (
            await self._outreach.is_suppressed(tenant_id, target, actor=actor)
            is not None
        )


@dataclass(frozen=True)
class HunterContactComposition:
    """启用 Hunter 所需的显式政策、凭证边界与固定主机传输。"""

    discovery_policy: ContactDiscoveryPolicyReader
    country_policy: ContactCountryPolicyReader
    secret_resolver: HunterSecretResolver
    secret_ref: str
    transport: HunterHttpTransport
    quota: ProviderQuotaGuard | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.discovery_policy, ContactDiscoveryPolicyReader)
            or not isinstance(self.country_policy, ContactCountryPolicyReader)
            or not isinstance(self.secret_resolver, HunterSecretResolver)
            or not isinstance(self.secret_ref, str)
            or not self.secret_ref
            or self.secret_ref != self.secret_ref.strip()
            or not isinstance(self.transport, HunterHttpTransport)
            or (
                self.quota is not None
                and not isinstance(self.quota, ProviderQuotaGuard)
            )
        ):
            raise ValidationError("scheduler Hunter 联系人工具依赖未完整配置")


@dataclass(frozen=True)
class HunterContactTools:
    enricher: ContactEnricher
    verifier: ContactVerifier


def build_hunter_contact_tools(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    tool_user: UserId,
    fingerprints: HmacFingerprintProvider,
    outreach: OutreachService,
    prospecting: ProspectingService,
    composition: HunterContactComposition,
    lease_duration: timedelta,
    now: Callable[[], datetime],
) -> HunterContactTools:
    """注册双工具及完整检查管线，返回 workflow-facing typed adapters。"""
    enrichment_slot = ContextLocalSingleResultSlot[ContactEnrichmentResult](
        "ceb", new_id
    )
    verification_slot = ContextLocalSingleResultSlot[EmailVerificationResult](
        "veb", new_id
    )
    bound_secrets = _BoundHunterSecretResolver(
        composition.secret_resolver, composition.secret_ref
    )

    def connector_factory(requested_tenant: TenantId) -> HunterConnector:
        if requested_tenant != tenant_id:
            raise ValidationError("Hunter connector 租户不匹配")
        return HunterConnector(composition.transport, now=now)

    registry = ToolRegistry()
    registry.register(
        CONTACT_ENRICH_MANIFEST,
        ContactEnrichmentHandler(
            HunterProviderContactEnricher(connector_factory, bound_secrets),
            enrichment_slot,
            fingerprints,
        ),
    )
    registry.register(
        CONTACT_VERIFY_MANIFEST,
        ContactVerificationHandler(
            HunterProviderContactVerifier(connector_factory, bound_secrets),
            verification_slot,
            fingerprints,
            now=now,
        ),
    )

    async def authorize(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
        del state
        return (
            ctx.tenant_id == tenant_id
            and ctx.user_id == tool_user
            and ctx.tool_id
            in {
                CONTACT_ENRICH_MANIFEST.tool_id,
                CONTACT_VERIFY_MANIFEST.tool_id,
            }
        )

    suppression = _OutreachContactSuppressionReader(outreach)
    quota = composition.quota or InMemoryHunterQuotaGuard()
    checks: dict[str, CheckStage] = {
        "tenant": ContactResourceTenantCheck(),
        "permission": PermissionCheck(authorize),
        "playbook": ContactEnrichmentPlaybookCheck(composition.discovery_policy),
        "country_policy": ContactCountryPolicyCheck(composition.country_policy),
        "suppression": ContactProviderSuppressionCheck(
            prospecting,
            suppression,
            now=now,
        ),
        "rate_limit": ContactProviderRateLimitCheck(quota, now=now),
    }

    def tool_uow(requested_tenant: TenantId) -> ToolGatewayUnitOfWork:
        return cast(
            ToolGatewayUnitOfWork,
            SqlAlchemyToolGatewayUnitOfWork(factory, requested_tenant, now=now),
        )

    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]
        checks,
        cast(ToolGatewayUnitOfWorkFactory, tool_uow),
        lease_duration=lease_duration,
        lease_owner="scheduler_hunter_contacts",
        now=now,
        id_factory=new_id,
    )
    return HunterContactTools(
        enricher=ToolGatewayContactEnricher(gateway, enrichment_slot, tool_user),
        verifier=ToolGatewayContactVerifier(gateway, verification_slot, tool_user),
    )


__all__ = (
    "HunterContactComposition",
    "HunterContactTools",
    "build_hunter_contact_tools",
)
