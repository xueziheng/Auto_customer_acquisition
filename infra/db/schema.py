"""可复用的数据库 schema 启动门禁。"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

_REPO_ROOT = Path(__file__).resolve().parents[2]


class DatabaseSchemaError(RuntimeError):
    """固定、脱敏的 schema 不一致错误。"""

    def __init__(self) -> None:
        super().__init__("数据库 schema 启动检查失败")


async def assert_database_schema_current(engine: AsyncEngine) -> None:
    """要求本地与数据库都恰好处于同一个 Alembic head。"""
    try:
        config = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
        config.set_main_option("path_separator", "os")
        local_heads = tuple(ScriptDirectory.from_config(config).get_heads())
        async with engine.connect() as connection:
            database_heads = tuple(
                await connection.run_sync(
                    lambda sync_connection: MigrationContext.configure(
                        sync_connection
                    ).get_current_heads()
                )
            )
    except Exception:  # noqa: BLE001 - 驱动/Alembic 细节统一固定映射
        raise DatabaseSchemaError() from None
    if len(local_heads) != 1 or database_heads != local_heads:
        raise DatabaseSchemaError()
