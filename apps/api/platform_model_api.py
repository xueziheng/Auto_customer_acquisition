"""平台模型设置与显式连接测试装配。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.model_api.client import CompatibleModelClient
from domains.organization.data_sources import DataSourceConflict, DataSourceUnavailable
from domains.organization.model_api import ModelApiCommand, ModelApiUpdate, ModelApiView
from domains.organization.platform_access import PlatformAccessService
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.model_api import FileModelApiRepository, ModelKeyFileResolver
from shared.authentication import AuthPrincipal
from shared.schemas.identifiers import IdempotencyKey, TenantId, new_id
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_connection_probe import (
    MODEL_CONNECTION_MANIFEST,
    ModelConnectionProbe,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
    _GatewayRegistry,
)
from tool_gateway.repository import ToolGatewayUnitOfWork


class PlatformModelApi:
    def __init__(self, tenant: TenantId, access: PlatformAccessService, store: FileModelApiRepository,
        sessions: async_sessionmaker | None = None, fingerprints: HmacFingerprintProvider | None = None) -> None:
        self._tenant, self._access, self._store = tenant, access, store
        self._sessions, self._fingerprints = sessions, fingerprints
        self._lock = asyncio.Lock()

    async def get(self, principal: AuthPrincipal) -> ModelApiView:
        await self._access.authorize(principal)
        return self._store.view(self._tenant)

    async def save(self, principal: AuthPrincipal, command: ModelApiUpdate) -> ModelApiView:
        await self._access.authorize(principal)
        return self._store.save(self._tenant, str(principal.user_id), command)

    async def apply(self, principal: AuthPrincipal, command: ModelApiCommand) -> ModelApiView:
        await self._access.authorize(principal)
        return self._store.request_apply(self._tenant, str(principal.user_id), command)

    async def test(self, principal: AuthPrincipal, command: ModelApiCommand, *, factory_builder=None, record_result=None, kind="api") -> ModelApiView:
        async with self._lock:
            config = await self.get(principal)
            if config.revision != command.expected_revision or config.apply_status == "pending":
                raise DataSourceConflict("配置已变化，请刷新")
            if not config.configured or config.configuration_version is None or self._sessions is None or self._fingerprints is None:
                raise DataSourceUnavailable("请先配置模型连接")
            if kind == "api" and config.tested_at and datetime.now(UTC) - config.tested_at < timedelta(seconds=60):
                return config
            tenant = self._tenant
            version = config.configuration_version
            key = f"modeltest:{kind}:{version}:{int(datetime.now(UTC).timestamp()) // 60}"

            async def authorize() -> None:
                current = await self.get(principal)
                if current.revision != config.revision or current.apply_status == "pending":
                    raise DataSourceConflict("配置已更新")

            async def permitted(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
                await authorize()
                return ctx.tenant_id == tenant and ctx.user_id == principal.user_id

            class Check:
                name = "tenant"
                async def check(self, ctx, state):
                    return None if ctx.tenant_id == tenant else CheckRejection("tenant", "model:tenant", "租户不匹配")

            class Rate:
                name = "rate_limit"
                async def check(self, ctx, state):
                    return None if ctx.idempotency_key == key else CheckRejection("rate_limit", "model:rate", "检查过于频繁")

            resolver = ModelKeyFileResolver(self._store.key_path(version), tenant, version, "MODEL_API_KEY")
            factory = (lambda: CompatibleModelClient("MODEL_API_KEY", resolver, base_url=config.base_url,
                    protocol=config.protocol, timeout_seconds=30)) if factory_builder is None else factory_builder(config, resolver)
            handler = ModelConnectionProbe(tenant, str(principal.user_id), version, key, authorize,
                factory, config.model, self._fingerprints)
            registry = ToolRegistry()
            registry.register(MODEL_CONNECTION_MANIFEST, handler)
            gateway = ToolGateway(cast(_GatewayRegistry, registry),
                {"tenant": Check(), "permission": PermissionCheck(permitted), "idempotency": IdempotencyCheck(), "rate_limit": Rate()},
                lambda tenant_id: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(self._sessions, tenant_id)),
                lease_duration=timedelta(seconds=120), lease_owner="platform_model_test", id_factory=new_id)
            try:
                result = await gateway.invoke(ToolCallContext(tenant, principal.user_id, MODEL_CONNECTION_MANIFEST.tool_id,
                    {"configuration_version": version}, idempotency_key=IdempotencyKey(key)))
                await authorize()
                if result.status is ToolCallStatus.DUPLICATE:
                    return await self.get(principal)
                if result.status is not ToolCallStatus.SUCCEEDED or handler.result is None:
                    raise DataSourceUnavailable("模型测试暂不可用")
                if record_result is not None:
                    record_result(config.revision, *handler.result)
                    return await self.get(principal)
                return self._store.record_test(tenant, config.revision, *handler.result)
            finally:
                handler.result = None
