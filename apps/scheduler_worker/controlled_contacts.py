"""受控外部端口的联系人工具装配；真实Gateway检查与本runtime域服务。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from connectors.contact_enrichment.client import ContactEnrichmentResult
from connectors.email_verification.client import EmailVerificationResult
from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import SuppressionTarget
from domains.outreach.service import OutreachService
from infra.controlled.contacts import ControlledAccountModel, ControlledContactProvider
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, new_id
from tool_gateway.checks.contact_provider import (
    ContactCountryPolicyCheck,
    ContactEnrichmentPlaybookCheck,
    ContactProviderRateLimitCheck,
    ContactProviderSuppressionCheck,
    ContactResourceTenantCheck,
    InMemoryHunterQuotaGuard,
)
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.handlers.contact_enrichment import MANIFEST as CONTACT_ENRICH_MANIFEST
from tool_gateway.handlers.contact_enrichment import (
    ContactEnrichmentHandler,
    ToolGatewayContactEnricher,
)
from tool_gateway.handlers.contact_verification import (
    MANIFEST as CONTACT_VERIFY_MANIFEST,
)
from tool_gateway.handlers.contact_verification import (
    ContactVerificationHandler,
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
from tool_gateway.repository import ToolGatewayUnitOfWork, ToolGatewayUnitOfWorkFactory

from .account_discovery import (
    ComplianceCountryPolicyDecisionReader,
    DemandProspectingContactDiscoveryPolicy,
)
from .bootstrap import ContactRuntimePorts
from .contact_binding import ContactRuntimeResources
from .runtime import SchedulerCoreServices


class ControlledOutreachSuppressionReader:
    def __init__(self, outreach: OutreachService) -> None:
        if not callable(getattr(outreach, "is_suppressed", None)):
            raise ValidationError("联系人抑制依赖无效")
        self._outreach = outreach

    async def is_suppressed(
        self, tenant_id: TenantId, target: SuppressionTarget
    ) -> bool:
        actor = OutreachActor(
            "system:scheduler-controlled-contacts",
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
class ControlledContactFactory:
    """仅外部fixture持有路径；业务服务全部从当前runtime借用。"""

    provider_path: Path

    def __call__(
        self,
        core: SchedulerCoreServices,
        outreach: OutreachService,
        resources: ContactRuntimeResources,
    ) -> ContactRuntimePorts:
        tenant_id, tool_user, now = (
            resources.tenant_id,
            resources.tool_user,
            resources.now,
        )
        provider = ControlledContactProvider(
            self.provider_path, tenant_id=tenant_id, now=now
        )
        enrichment_slot = ContextLocalSingleResultSlot[ContactEnrichmentResult](
            "ceb", new_id
        )
        verification_slot = ContextLocalSingleResultSlot[EmailVerificationResult](
            "veb", new_id
        )
        registry = ToolRegistry()
        registry.register(
            CONTACT_ENRICH_MANIFEST,
            ContactEnrichmentHandler(provider, enrichment_slot, resources.fingerprints),
        )
        registry.register(
            CONTACT_VERIFY_MANIFEST,
            ContactVerificationHandler(
                provider, verification_slot, resources.fingerprints, now=now
            ),
        )
        expected_ids = ("contact.enrich", "contact.verify")

        async def authorize(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
            return (
                ctx.tenant_id == tenant_id
                and ctx.user_id == tool_user
                and ctx.tool_id in expected_ids
            )

        checks: dict[str, CheckStage] = {
            "tenant": ContactResourceTenantCheck(),
            "permission": PermissionCheck(authorize),
            "playbook": ContactEnrichmentPlaybookCheck(
                DemandProspectingContactDiscoveryPolicy(core.demand, core.prospecting)
            ),
            "country_policy": ContactCountryPolicyCheck(
                ComplianceCountryPolicyDecisionReader(
                    core.compliance,
                    ComplianceActor(
                        "system:controlled-contacts",
                        tenant_id,
                        ComplianceScope.SYSTEM,
                        "system",
                    ),
                )
            ),
            "suppression": ContactProviderSuppressionCheck(
                core.prospecting, ControlledOutreachSuppressionReader(outreach), now=now
            ),
            "rate_limit": ContactProviderRateLimitCheck(
                InMemoryHunterQuotaGuard(), now=now
            ),
        }

        def tool_uow(requested: TenantId) -> ToolGatewayUnitOfWork:
            return cast(
                ToolGatewayUnitOfWork,
                SqlAlchemyToolGatewayUnitOfWork(resources.sessions, requested, now=now),
            )

        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]
            checks,
            cast(ToolGatewayUnitOfWorkFactory, tool_uow),
            lease_duration=resources.lease_duration,
            lease_owner="scheduler_controlled_contacts",
            now=now,
            id_factory=new_id,
        )
        return ContactRuntimePorts(
            ControlledAccountModel(),
            "controlled-account-v1",
            ("KE",),
            ToolGatewayContactEnricher(gateway, enrichment_slot, tool_user),
            ToolGatewayContactVerifier(gateway, verification_slot, tool_user),
        )
