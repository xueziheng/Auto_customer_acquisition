"""真实浏览器栈的库存装配；公共目录独立租户，采集来源保持未配置。"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.inventory import bind_inventory
from apps.api.identity import RequestIdentity
from apps.api.runtime import create_runtime_app
from domains.prospecting.service import InventoryService
from infra.db.inventory_sources import InventorySources
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.runtime_scope import verify_runtime_database_scope
from infra.db.search_quota import PostgresSearchQuotaRepository
from infra.db.session import create_engine_from
from shared.schemas.identifiers import TenantId


def create_e2e_runtime_app() -> FastAPI:
    """保留原 runtime 门禁及生命周期，为测试增加真实库存和独立控制面连接。

    环境仅来自本次隔离 fixture；不加载生产配置或构造 Provider。两个 engine
    各自绑定已验证的租户数据库身份，并在启动失败及正常退出时释放。
    """
    tenant_id = TenantId(os.environ["TRADEOS_TENANT_ID"])
    control_tenant = TenantId(os.environ["E2E_CONTROL_TENANT_ID"])
    if control_tenant == tenant_id:
        raise ValueError("E2E 公共目录和企业租户必须独立")
    app = create_runtime_app()
    control_engine = create_engine_from(os.environ["E2E_CONTROL_DATABASE_URL"])
    sessions = async_sessionmaker(app.state.runtime_engine, expire_on_commit=False)
    catalog = PostgresPublicCatalog(
        async_sessionmaker(control_engine, expire_on_commit=False), control_tenant,
    )
    inventory = PostgresInventoryService(sessions, tenant_id, catalog)

    def inventory_factory(identity: RequestIdentity) -> InventoryService:
        """按当前请求员工重新授权，不能缓存员工权限快照。"""
        return bind_inventory(inventory, app.state.dependencies, identity)

    app.state.prospect_inventory_factory = inventory_factory
    app.state.inventory_sources = InventorySources(
        tenant_id=tenant_id,
        backend=inventory,
        quota=PostgresSearchQuotaRepository(
            sessions, tenant_id, now=lambda: datetime.now(UTC),
        ),
        configured=False,
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """沿用业务资源清理；独立目录连接在外层 finally 精确释放。"""
        try:
            async with original_lifespan(application):
                await verify_runtime_database_scope(control_engine, str(control_tenant))
                yield
        finally:
            await control_engine.dispose()

    app.router.lifespan_context = lifespan
    return app
