"""搜索绑定迁移往返及继承原表强制RLS。"""
from uuid import uuid4

from sqlalchemy import inspect, text

from infra.db.session import create_engine_from
from infra.db.tables import Base
from tests.integration.test_migrations import _run_alembic

pytest_plugins = ("tests.integration.test_outreach_campaign_lifecycle",)


async def test_search_backend_migration_roundtrip(lifecycle_engine):
    name = "test_search_backend_" + uuid4().hex
    async with lifecycle_engine.connect() as conn:
        admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{name}"'))
    url = lifecycle_engine.url.set(database=name).render_as_string(False)
    engine = create_engine_from(url)
    try:
        for revision in ("0092", "0091", "0092"):
            _run_alembic(url, "downgrade" if revision == "0091" else "upgrade", revision)
            async with engine.connect() as conn:
                columns = await conn.run_sync(lambda c: inspect(c).get_columns("acquisition_jobs"))
                names = {c["name"] for c in columns}
                assert ("search_binding" in names) == (revision == "0092")
                assert await conn.scalar(text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname='acquisition_jobs'"))
                if revision == "0092":
                    assert {c["name"]: c["nullable"] for c in columns} == {c.name: c.nullable for c in Base.metadata.tables["acquisition_jobs"].columns}
    finally:
        await engine.dispose()
        async with lifecycle_engine.connect() as conn:
            admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
