"""归属版本迁移往返保留强制租户策略，已有任务禁止有损降级。"""
from sqlalchemy import inspect, text

from infra.db.session import create_engine_from
from tests.integration.test_migrations import _run_alembic
from tests.migration_database_fixtures import (
    migration_database_url as migration_database_url,  # noqa: PLC0414
)


async def test_preparation_ownership_migration_roundtrip(migration_database_url):
    engine = create_engine_from(migration_database_url)
    try:
        _run_alembic(migration_database_url, "downgrade", "0090")
        async with engine.connect() as conn:
            assert "ownership_locked_at" not in {x["name"] for x in await conn.run_sync(lambda c: inspect(c).get_columns("customer_preparation_items"))}
        _run_alembic(migration_database_url, "upgrade", "head")
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: inspect(c).get_columns("customer_preparation_items"))
            assert next(x for x in columns if x["name"] == "ownership_locked_at")["nullable"]
            assert (await conn.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='customer_preparation_items'"))).one() == (True, True)
    finally:
        await engine.dispose()
