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
from connectors.hunter.secrets import BoundHunterSecretResolver
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
    ContactDiscoveryPolicyReader,
    ContactEnrichmentPlaybookCheck,
    ContactProviderRateLimitCheck,
    ContactProviderSuppressionCheck,
    ContactResourceTenantCheck,
    CountryPolicyDecisionReader,
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
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderId,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
    ProviderRuntimeGuard,
)
from tool_gateway.repository import (
    ToolGatewayUnitOfWork,
    ToolGatewayUnitOfWorkFactory,
)
from workflows.account_discovery.ports import ContactEnricher, ContactVerifier


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
    """Hunter 联系人业务检查依赖；凭证与传输由生产组合显式传入。"""

    discovery_policy: ContactDiscoveryPolicyReader
    quota: ProviderQuotaGuard | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.discovery_policy, ContactDiscoveryPolicyReader) or (
            self.quota is not None and not isinstance(self.quota, ProviderQuotaGuard)
        ):
            raise ValidationError("scheduler Hunter 联系人工具依赖未完整配置")


@dataclass(frozen=True)
class HunterContactTools:
    enricher: ContactEnricher
    verifier: ContactVerifier
    registered_configuration_hash: str | None
    manifest_ids: tuple[str, ...]


def build_hunter_contact_tools(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    tool_user: UserId,
    fingerprints: HmacFingerprintProvider,
    outreach: OutreachService,
    prospecting: ProspectingService,
    country_policy: CountryPolicyDecisionReader,
    composition: HunterContactComposition,
    secret_resolver: HunterSecretResolver,
    secret_ref: str,
    transport: HunterHttpTransport,
    snapshot: ProviderReadinessSnapshot | None,
    readiness_guard: ProviderRuntimeGuard,
    lease_duration: timedelta,
    now: Callable[[], datetime],
) -> HunterContactTools:
    """仅对当前已验证的精确配置注册两个 Hunter 工具。"""
    if (
        not isinstance(secret_resolver, HunterSecretResolver)
        or not isinstance(secret_ref, str)
        or not isinstance(transport, HunterHttpTransport)
        or not isinstance(readiness_guard, ProviderRuntimeGuard)
    ):
        raise ValidationError("scheduler Hunter 运行依赖无效")
    enrichment_slot = ContextLocalSingleResultSlot[ContactEnrichmentResult](
        "ceb", new_id
    )
    verification_slot = ContextLocalSingleResultSlot[EmailVerificationResult](
        "veb", new_id
    )

    registry = ToolRegistry()
    registered_configuration_hash = _registered_configuration_hash(snapshot, tenant_id)
    if registered_configuration_hash is not None:
        bound_secrets = BoundHunterSecretResolver(secret_resolver, secret_ref)

        def connector_factory(requested_tenant: TenantId) -> HunterConnector:
            if requested_tenant != tenant_id:
                raise ValidationError("Hunter connector 租户不匹配")
            return HunterConnector(transport, now=now)

        registry.register(
            CONTACT_ENRICH_MANIFEST,
            ContactEnrichmentHandler(
                HunterProviderContactEnricher(
                    connector_factory,
                    bound_secrets,
                    readiness_guard,
                    registered_configuration_hash,
                ),
                enrichment_slot,
                fingerprints,
            ),
        )
        registry.register(
            CONTACT_VERIFY_MANIFEST,
            ContactVerificationHandler(
                HunterProviderContactVerifier(
                    connector_factory,
                    bound_secrets,
                    readiness_guard,
                    registered_configuration_hash,
                ),
                verification_slot,
                fingerprints,
                now=now,
            ),
        )

    manifest_ids = tuple(item.tool_id for item in registry.list_manifests())
    expected_ids = (
        ()
        if registered_configuration_hash is None
        else (CONTACT_ENRICH_MANIFEST.tool_id, CONTACT_VERIFY_MANIFEST.tool_id)
    )
    if manifest_ids != expected_ids:
        raise ValidationError("scheduler Hunter registry 无效")

    async def authorize(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
        del state
        return (
            ctx.tenant_id == tenant_id
            and ctx.user_id == tool_user
            and ctx.tool_id in expected_ids
        )

    suppression = _OutreachContactSuppressionReader(outreach)
    quota = composition.quota or InMemoryHunterQuotaGuard()
    checks: dict[str, CheckStage] = {
        "tenant": ContactResourceTenantCheck(),
        "permission": PermissionCheck(authorize),
        "playbook": ContactEnrichmentPlaybookCheck(composition.discovery_policy),
        "country_policy": ContactCountryPolicyCheck(country_policy),
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
        registered_configuration_hash=registered_configuration_hash,
        manifest_ids=manifest_ids,
    )


def _registered_configuration_hash(
    snapshot: object,
    tenant_id: TenantId,
) -> str | None:
    if (
        not isinstance(snapshot, ProviderReadinessSnapshot)
        or snapshot.tenant_id != tenant_id
        or snapshot.provider is not ProviderId.HUNTER
        or snapshot.capabilities != HUNTER_CONTACT_CAPABILITIES
        or snapshot.configuration is None
        or snapshot.state
        not in {
            ProviderReadinessState.RUNTIME_NOT_COMPOSED,
            ProviderReadinessState.READY,
        }
    ):
        return None
    return snapshot.configuration.configuration_hash


__all__ = (
    "HunterContactComposition",
    "HunterContactTools",
    "build_hunter_contact_tools",
)
