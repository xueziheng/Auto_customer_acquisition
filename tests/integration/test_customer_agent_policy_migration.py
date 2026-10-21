"""独立测试库验证政策列往返，保留现有租户 RLS。"""
from uuid import uuid4

from sqlalchemy import inspect, text

from infra.db.session import create_engine_from
from infra.db.tables import Base
from tests.integration.test_migrations import _run_alembic

pytest_plugins = ("tests.integration.test_outreach_campaign_lifecycle",)


async def test_policy_migration_roundtrip(lifecycle_engine):
    database = "test_agent_policy_" + uuid4().hex
    async with lifecycle_engine.connect() as conn:
        admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{database}"'))
    url = lifecycle_engine.url.set(database=database).render_as_string(False)
    engine = create_engine_from(url)
    try:
        _run_alembic(url, "upgrade", "0089")
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: inspect(c).get_columns("outreach_campaign_versions"))
            assert {c["name"]: c["nullable"] for c in columns} == {
                c.name: c.nullable for c in Base.metadata.tables["outreach_campaign_versions"].columns
            }
            assert await conn.scalar(text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname='outreach_campaign_versions'"))
        _run_alembic(url, "downgrade", "0088")
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: inspect(c).get_columns("outreach_campaign_versions"))
            assert "customer_agent_policy" not in {c["name"] for c in columns}
        _run_alembic(url, "upgrade", "0089")
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: inspect(c).get_columns("outreach_campaign_versions"))
            assert "customer_agent_policy" in {c["name"] for c in columns}
    finally:
        await engine.dispose()
        async with lifecycle_engine.connect() as conn:
            admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
