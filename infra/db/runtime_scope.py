"""运行入口必须通过企业数据库身份与行级隔离门禁。"""

from sqlalchemy.ext.asyncio import AsyncEngine

from infra.db.tenant_security import assert_tenant_database_isolation


async def verify_runtime_database_scope(engine: AsyncEngine, tenant_id: str) -> None:
    """拒绝管理账户、缺失配置与不完整隔离；不得以兼容模式跳过门禁。

    数据库迁移使用独立的管理连接，不经过应用运行入口。
    """
    await assert_tenant_database_isolation(engine, tenant_id)
