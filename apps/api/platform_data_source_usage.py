"""独立平台额度检查装配；外部读取只经 Gateway，使用免费账户端点。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.hunter.client import HUNTER_API_KEY_REF
from connectors.hunter.transport import HunterApiHttpTransport, HunterHttpTransport
from connectors.hunter.usage import HunterUsageConnector
from domains.organization.data_source_usage import usage_view
from domains.organization.data_sources import (
    DataSourceConflict,
    DataSourceInputInvalid,
    DataSourceUnavailable,
)
from domains.organization.platform_access import PlatformAccessService
from domains.organization.schemas import DataSourceConfigurationView
from infra.data_source_credentials import FileDataSourceConfigurationRepository
from infra.data_source_usage import FileDataSourceUsageRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.authentication import AuthPrincipal
from shared.schemas.identifiers import IdempotencyKey, TenantId, new_id
from shared.schemas.provider_usage import ProviderUsageObservation
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.provider_usage import (
    PROVIDER_USAGE_MANIFEST,
    ProviderUsageHandler,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)


class PlatformDataSourceUsage:
    """API 进程实例独享锁；跨进程相同分钟检查由持久幂等键合并。"""

    def __init__(
        self, tenant: TenantId, access: PlatformAccessService,
        credentials: FileDataSourceConfigurationRepository, directory: Path,
        sessions: async_sessionmaker[AsyncSession], fingerprints: HmacFingerprintProvider,
        *, transport: HunterHttpTransport | None = None,
    ) -> None:
        self._tenant, self._access, self._credentials = tenant, access, credentials
        self._sessions, self._fingerprints = sessions, fingerprints
        self._store = FileDataSourceUsageRepository(directory, tenant, credentials)
        self._transport = transport or HunterApiHttpTransport()
        self._lock = asyncio.Lock()

    def supports(self, source_id: str) -> bool:
        return source_id == "hunter"

    def snapshot(self, tenant: TenantId, source_id: str, revision: int) -> ProviderUsageObservation | None:
        if tenant != self._tenant:
            raise DataSourceUnavailable("额度检查不可用")
        return self._store.read(tenant, source_id, revision) if self.supports(source_id) else None

    async def check(self, principal: AuthPrincipal, source_id: str, configuration: DataSourceConfigurationView) -> None:
        """缓存至少一分钟；免费读取失败也记录真实状态，不无限循环或付费探测。"""
        if not self.supports(source_id) or not configuration.configured or not configuration.configuration_version:
            raise DataSourceInputInvalid("额度查询尚未接入")

        async def authorize() -> None:
            await self._access.authorize(principal)
            current = self._credentials.list_configurations(self._tenant).get(source_id)
            if current is None or not current.configured or current.revision != configuration.revision:
                raise DataSourceConflict("配置已更新")

        async with self._lock:
            await authorize()
            cached = self.snapshot(self._tenant, source_id, configuration.revision)
            view = usage_view(cached)
            if view.next_check_at is not None and datetime.now(UTC) < view.next_check_at:
                return
            tenant = self._tenant
            key = f"usage:{configuration.configuration_version}:{int(datetime.now(UTC).timestamp()) // 60}"
            credentials = self._credentials

            class Resolver:
                def resolve(self, reference: str) -> str:
                    if reference != HUNTER_API_KEY_REF:
                        raise DataSourceInputInvalid("额度查询引用无效")
                    return credentials.resolve_credential(tenant, source_id, "api_key", revision=configuration.revision).get_secret_value()

            class TenantCheck:
                name = "tenant"

                async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
                    return None if ctx.tenant_id == tenant else CheckRejection("tenant", "usage:tenant", "租户不匹配")

            class RateCheck:
                name = "rate_limit"

                async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
                    return None if ctx.idempotency_key == key else CheckRejection("rate_limit", "usage:window", "检查过于频繁")

            async def permitted(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
                if ctx.tenant_id != tenant or ctx.user_id != principal.user_id:
                    return False
                await authorize()
                return True

            handler = ProviderUsageHandler(
                tenant, str(principal.user_id), configuration.configuration_version, key, authorize,
                HunterUsageConnector(self._transport, Resolver()), self._fingerprints,
            )
            registry = ToolRegistry()
            registry.register(PROVIDER_USAGE_MANIFEST, handler)
            def unit_of_work(tenant_id: TenantId) -> SqlAlchemyToolGatewayUnitOfWork:
                return SqlAlchemyToolGatewayUnitOfWork(self._sessions, tenant_id)

            gateway = ToolGateway(
                registry,  # type: ignore[arg-type]
                {"tenant": TenantCheck(), "permission": PermissionCheck(permitted), "idempotency": IdempotencyCheck(), "rate_limit": RateCheck()},
                unit_of_work,  # type: ignore[arg-type]
                lease_duration=timedelta(seconds=60), lease_owner="platform_usage", id_factory=new_id,
            )
            try:
                result = await gateway.invoke(ToolCallContext(
                    tenant, principal.user_id, PROVIDER_USAGE_MANIFEST.tool_id,
                    {"configuration_version": configuration.configuration_version}, idempotency_key=IdempotencyKey(key),
                ))
                await authorize()
                if result.status is ToolCallStatus.DUPLICATE:
                    return
                if result.status is not ToolCallStatus.SUCCEEDED or handler.observation is None or result.tool_call_id is None:
                    raise DataSourceUnavailable("额度检查暂不可用")
                self._store.record(
                    tenant, source_id, configuration.revision, actor_id=str(principal.user_id),
                    tool_call_id=str(result.tool_call_id), observation=handler.observation,
                )
            finally:
                handler.observation = None
