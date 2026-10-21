"""单客户验证部署装配：精确旧配置、惰性凭证、租户持久预算。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.hunter.transport import HunterApiHttpTransport
from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.compliance.schemas import CountryPolicyAction, CountryPolicyDecision
from domains.compliance.service import ComplianceService
from domains.demand.service import DemandService
from domains.prospecting.service import ProspectingService
from infra.db.customer_followup import PostgresCustomerContextReader
from infra.db.hunter_customer_quota import (
    PostgresHunterCustomerQuota as PostgresHunterCustomerQuota,  # noqa: PLC0414
)
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.standalone.hunter_settings import HunterCustomerSettings
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
)
from tool_gateway.checks.contact_provider import (
    ContactDiscoveryPreflight,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.provider_readiness import (
    ProviderConfiguration,
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
)

from .customer_verification import (
    CustomerVerificationApplication,
    RecordedContactBasisPolicy,
)
from .customer_verification_runtime import ConfiguredHunterCustomerVerificationRuntime

if TYPE_CHECKING:
    from ..dependencies import ConfiguredApiDependencies

from infra.hunter_customer_credentials import (
    HUNTER_CUSTOMER_KEY as HUNTER_CUSTOMER_KEY,  # noqa: PLC0414
)
from infra.hunter_customer_credentials import (
    HunterCustomerCredentialReader as HunterCustomerCredentialReader,  # noqa: PLC0414
)
from infra.hunter_customer_credentials import (
    HunterCustomerSecretResolver as HunterCustomerSecretResolver,  # noqa: PLC0414
)
from infra.hunter_customer_credentials import (
    PlatformHunterCustomerSecretResolver as PlatformHunterCustomerSecretResolver,  # noqa: PLC0414
)


class DemandProspectingContactDiscoveryPolicy:
    """只通过两域公共服务组装已绑定企业与当前可发现假设。"""

    def __init__(self, demand: DemandService, prospecting: ProspectingService) -> None:
        self._demand, self._prospecting = demand, prospecting

    async def preflight(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
    ) -> ContactDiscoveryPreflight:
        hypothesis = await self._demand.get_hypothesis_for_discovery(
            tenant_id, hypothesis_id
        )
        if hypothesis.account_id != str(account_id):
            raise ValidationError("需求假设与目标企业不匹配")
        detail = await self._prospecting.get_account_detail(tenant_id, account_id)
        account = detail.account
        if account.tenant_id != tenant_id or account.account_id != account_id:
            raise TenantIsolationViolation("联系人发现企业租户绑定无效")
        if account.website_domain is None:
            raise ValidationError("联系人发现企业缺少官网域名")
        return ContactDiscoveryPreflight(
            tenant_id,
            hypothesis_id,
            account_id,
            hypothesis.category,
            account.country,
            account.website_domain,
        )


class ComplianceCountryPolicyDecisionReader:
    """具名 SYSTEM actor 只调用合规公共决策，不解释或推断国家许可。"""

    def __init__(self, compliance: ComplianceService, tenant_id: TenantId) -> None:
        self._compliance = compliance
        self._actor = ComplianceActor(
            "system:customer-verification", tenant_id, ComplianceScope.SYSTEM, "system"
        )

    async def decision(
        self, tenant_id: TenantId, country: str, action: CountryPolicyAction
    ) -> CountryPolicyDecision:
        if tenant_id != self._actor.tenant_id:
            raise TenantIsolationViolation("联系人国家政策租户绑定无效")
        return await self._compliance.get_country_policy_decision(
            tenant_id, country, action, actor=self._actor
        )


def build_standalone_customer_verification(
    factory: async_sessionmaker[AsyncSession],
    dependencies: ConfiguredApiDependencies,
    *,
    tenant_id: TenantId,
    settings: HunterCustomerSettings | None,
    fingerprints: HmacFingerprintProvider,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CustomerVerificationApplication:
    """构造零外部IO；startup才组合精确已有验证，绝不声明或测试Provider。"""
    tenant = tenant_id
    prospecting = dependencies.prospecting
    if prospecting is None:
        raise ValidationError("客户验证档案服务未配置")
    reader = PostgresCustomerContextReader(factory, tenant)
    if settings is None or not settings.enabled:
        return CustomerVerificationApplication(reader, prospecting)
    if settings.business_tenant_id != tenant:
        raise TenantIsolationViolation("Hunter 客户验证企业配置不一致")
    demand, compliance = dependencies.demand_service, dependencies.compliance
    if demand is None or compliance is None:
        raise ValidationError("Hunter 客户验证公共政策服务未配置")
    actor = ProviderReadinessActor(
        "system:customer-verification",
        tenant,
        frozenset(
            {ProviderReadinessPermission.READ, ProviderReadinessPermission.COMPOSE}
        ),
    )
    readiness = ProviderReadinessServiceImpl(
        lambda t: SqlAlchemyProviderReadinessUnitOfWork(factory, t, now=now),
        runtime_actor=actor,
        now=now,
    )
    secret_resolver: HunterCustomerSecretResolver | PlatformHunterCustomerSecretResolver
    if settings.credential_source == "platform":
        secret_resolver = PlatformHunterCustomerSecretResolver(settings)
    else:
        if settings.secret_file is None:
            raise ValidationError("Hunter 凭证来源配置无效")
        secret_resolver = HunterCustomerSecretResolver(settings.secret_file)
    runtime = ConfiguredHunterCustomerVerificationRuntime(
        factory=factory,
        tenant_id=tenant,
        configuration=ProviderConfiguration.hunter_contacts(
            settings.configuration_version, settings.api_key_version
        ),
        readiness=readiness,
        readiness_actor=actor,
        prospecting=prospecting,
        outreach=dependencies.outreach,
        country_policy=ComplianceCountryPolicyDecisionReader(compliance, tenant),
        discovery_policy=DemandProspectingContactDiscoveryPolicy(demand, prospecting),
        quota=PostgresHunterCustomerQuota(
            factory,
            tenant,
            maximum_daily_verifications=settings.maximum_daily_verifications,
            now=now,
        ),
        secret_resolver=secret_resolver,
        secret_ref=HUNTER_CUSTOMER_KEY,
        transport=HunterApiHttpTransport(),
        fingerprints=fingerprints,
        now=now,
    )
    return CustomerVerificationApplication(
        reader, prospecting, runtime, policy=RecordedContactBasisPolicy()
    )
