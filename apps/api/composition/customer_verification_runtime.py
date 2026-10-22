"""只复用已验证的精确 Hunter 配置；不声明配置或自动发起账户测试。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.composition_support.hunter_contacts import (
    HunterContactComposition,
    HunterContactTools,
    build_hunter_contact_tools,
)
from connectors.hunter.client import HunterSecretResolver
from connectors.hunter.transport import HunterHttpTransport
from domains.outreach.service import OutreachService
from domains.prospecting.service import ProspectingService
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    IdempotencyKey,
    ProspectAccountId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.checks.contact_provider import (
    ContactDiscoveryPolicyReader,
    CountryPolicyDecisionReader,
    ProviderQuotaGuard,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderConfiguration,
    ProviderReadinessActor,
    ProviderReadinessService,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
    ProviderRuntimeGuard,
)
from workflows.account_discovery.ports import ContactVerifier

from ..identity import RequestIdentity


class ConfiguredHunterCustomerVerificationRuntime:
    """配置与凭证引用由部署注入；构造无IO，compose仅写真实双工具组合事实。"""

    def __init__(
        self, *, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId,
        configuration: ProviderConfiguration, readiness: ProviderReadinessService,
        readiness_actor: ProviderReadinessActor, prospecting: ProspectingService,
        outreach: OutreachService, country_policy: CountryPolicyDecisionReader,
        discovery_policy: ContactDiscoveryPolicyReader, quota: ProviderQuotaGuard,
        secret_resolver: HunterSecretResolver, secret_ref: str, transport: HunterHttpTransport,
        fingerprints: HmacFingerprintProvider,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if readiness_actor.tenant_id != tenant_id or not isinstance(quota, ProviderQuotaGuard):
            raise ValidationError("Hunter runtime 租户绑定无效")
        self._factory, self._tenant, self._configuration = factory, tenant_id, configuration
        self._readiness, self._actor, self._prospecting = readiness, readiness_actor, prospecting
        self._outreach, self._country, self._discovery, self._quota = outreach, country_policy, discovery_policy, quota
        self._resolver, self._secret_ref, self._transport = secret_resolver, secret_ref, transport
        self._fingerprints, self._now = fingerprints, now
        self._composed = False

    async def _snapshot(self, tenant_id: TenantId) -> ProviderReadinessSnapshot:
        if tenant_id != self._tenant:
            raise PermissionDenied("Hunter runtime 企业范围不一致")
        return await self._readiness.get_snapshot(tenant_id, HUNTER_CONTACT_CAPABILITIES, actor=self._actor)

    def _configuration_matches(self, snapshot: ProviderReadinessSnapshot) -> bool:
        return (snapshot.tenant_id == self._tenant and snapshot.configuration == self._configuration
                and snapshot.capabilities == HUNTER_CONTACT_CAPABILITIES)

    async def blocking_reasons(self, tenant_id: TenantId) -> tuple[str, ...]:
        snapshot = await self._snapshot(tenant_id)
        if not self._configuration_matches(snapshot):
            return ("provider_configuration_changed",)
        if snapshot.state not in {ProviderReadinessState.READY, ProviderReadinessState.RUNTIME_NOT_COMPOSED}:
            return (snapshot.state.value,)
        if not self._composed or snapshot.state is not ProviderReadinessState.READY:
            return ("runtime_not_composed",)
        return ()

    def _tools(self, user_id: UserId, snapshot: ProviderReadinessSnapshot) -> HunterContactTools:
        tools = build_hunter_contact_tools(
            factory=self._factory, tenant_id=self._tenant, tool_user=user_id,
            fingerprints=self._fingerprints, outreach=self._outreach, prospecting=self._prospecting,
            country_policy=self._country,
            composition=HunterContactComposition(self._discovery, self._quota),
            secret_resolver=self._resolver, secret_ref=self._secret_ref, transport=self._transport,
            snapshot=snapshot, readiness_guard=cast(ProviderRuntimeGuard, self._readiness),
            lease_duration=timedelta(seconds=60), now=self._now,
        )
        if (tools.manifest_ids != ("contact.enrich", "contact.verify")
                or tools.registered_configuration_hash != self._configuration.configuration_hash):
            raise ValidationError("Hunter 双工具运行组合不完整")
        return tools

    async def compose(self) -> None:
        """先真实构造双工具，再追加组合事实；无真实验证历史时拒绝且不访问Provider。"""
        snapshot = await self._snapshot(self._tenant)
        if (not self._configuration_matches(snapshot) or snapshot.state not in {
                ProviderReadinessState.RUNTIME_NOT_COMPOSED, ProviderReadinessState.READY}):
            raise ValidationError("Hunter 精确配置尚未验证")
        self._tools(UserId(new_id("usr")), snapshot)
        if snapshot.state is ProviderReadinessState.RUNTIME_NOT_COMPOSED:
            await self._readiness.mark_runtime_composed(
                self._tenant, self._configuration.configuration_hash, actor=self._actor,
                idempotency_key=IdempotencyKey("api-customer-verification:" + self._configuration.configuration_hash),
            )
        self._composed = True

    async def verifier_for(
        self, identity: RequestIdentity, account_id: ProspectAccountId, contact_point_id: ContactPointId,
    ) -> ContactVerifier:
        if await self.blocking_reasons(identity.tenant_id):
            raise ValidationError("Hunter 当前运行配置不可用")
        point = await self._prospecting.get_contact_point(identity.tenant_id, contact_point_id)
        if point.tenant_id != identity.tenant_id or point.account_id != account_id:
            raise PermissionDenied("联系方式不属于当前客户")
        snapshot = await self._snapshot(identity.tenant_id)
        if not self._configuration_matches(snapshot) or snapshot.state is not ProviderReadinessState.READY:
            raise ValidationError("Hunter 当前运行配置已改变")
        user = UserId(identity.employee.user_id or str(identity.employee.employee_id))
        return self._tools(user, snapshot).verifier
