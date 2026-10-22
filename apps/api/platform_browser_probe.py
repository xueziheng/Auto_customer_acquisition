"""独立装配 UNGM 登录检查；只经 Gateway 调用隔离浏览器服务。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.playwright.login_probe import UngmLoginProbeConnector
from domains.organization.platform_access import PlatformAccessService
from infra.browser_probe import FileBrowserProbeRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.authentication import AuthPrincipal
from shared.schemas.browser_probe import BrowserProbeConnector, BrowserProbeObservation
from shared.schemas.identifiers import IdempotencyKey, TenantId, new_id
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.browser_probe import (
    BROWSER_PROBE_MANIFEST,
    BrowserProbeHandler,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)


class PlatformBrowserProbe:
    """仅受信部署启用；一分钟合并重复点击，读取目录不触发外部网络。"""
    def __init__(
        self, tenant: TenantId, access: PlatformAccessService, directory: Path,
        sessions: async_sessionmaker[AsyncSession], fingerprints: HmacFingerprintProvider,
        *, connector: BrowserProbeConnector | None = None,
    ) -> None:
        self._tenant, self._access = tenant, access
        self._sessions, self._fingerprints = sessions, fingerprints
        self._store = FileBrowserProbeRepository(directory, tenant)
        self._connector = connector or UngmLoginProbeConnector()
        self._lock = asyncio.Lock()

    def snapshot(self, tenant: TenantId) -> BrowserProbeObservation | None:
        return self._store.read(tenant)

    async def check(self, principal: AuthPrincipal) -> None:
        """审计成功才发布有效状态；故障覆盖旧成功，不进行自动重试。"""
        async def authorize() -> None:
            await self._access.authorize(principal)

        async with self._lock:
            await authorize()
            cached = self.snapshot(self._tenant)
            now = datetime.now(UTC)
            if cached is not None and timedelta(0) <= now - cached.checked_at < timedelta(minutes=1):
                return
            tenant = self._tenant
            key = f"ungm-login:{int(now.timestamp()) // 60}"

            class TenantCheck:
                name = "tenant"
                async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
                    return None if ctx.tenant_id == tenant else CheckRejection("tenant", "browser:tenant", "租户不匹配")

            class RateCheck:
                name = "rate_limit"
                async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
                    return None if ctx.idempotency_key == key else CheckRejection("rate_limit", "browser:window", "检查过于频繁")

            async def permitted(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
                if ctx.tenant_id != tenant or ctx.user_id != principal.user_id:
                    return False
                await authorize()
                return True

            handler = BrowserProbeHandler(tenant, str(principal.user_id), "ungm-server-v1", key, authorize, self._connector, self._fingerprints)
            registry = ToolRegistry()
            registry.register(BROWSER_PROBE_MANIFEST, handler)

            def unit_of_work(tenant_id: TenantId) -> SqlAlchemyToolGatewayUnitOfWork:
                return SqlAlchemyToolGatewayUnitOfWork(self._sessions, tenant_id)

            gateway = ToolGateway(
                registry,  # type: ignore[arg-type]
                {"tenant": TenantCheck(), "permission": PermissionCheck(permitted), "idempotency": IdempotencyCheck(), "rate_limit": RateCheck()},
                unit_of_work,  # type: ignore[arg-type]
                lease_duration=timedelta(seconds=120), lease_owner="platform_browser_probe", id_factory=new_id,
            )
            # 在访问外部前撤下旧成功，进程被终止也不会继续宣称旧登录有效。
            self._store.record(tenant, str(principal.user_id), None, BrowserProbeObservation(outcome="unavailable", checked_at=now))
            try:
                result = await gateway.invoke(ToolCallContext(
                    tenant, principal.user_id, BROWSER_PROBE_MANIFEST.tool_id,
                    {"configuration_version": "ungm-server-v1"}, idempotency_key=IdempotencyKey(key),
                ))
                await authorize()
                if result.status is ToolCallStatus.DUPLICATE:
                    return
                if result.status is not ToolCallStatus.SUCCEEDED or handler.observation is None or result.tool_call_id is None:
                    return
                self._store.record(tenant, str(principal.user_id), str(result.tool_call_id), handler.observation)
            finally:
                handler.observation = None
