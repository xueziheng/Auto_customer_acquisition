"""0077用途不能丢失；独占fixture库校验实际schema和ORM。"""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import PrivateReplyTestRow
from shared.schemas.identifiers import new_id
from tests.integration.test_qq_inbound_scope import seed
from tests.runtime_database_fixtures import (
    runtime_database_url as runtime_database_url,  # noqa: PLC0414
)


async def test_used_private_purpose_refuses_downgrade_and_matches_orm(db_url):
    engine = create_engine_from(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = new_id("tn")
    try:
        await seed(sessions, tenant)
        path = Path(__file__).parents[2] / "migrations/versions/0077_private_reply_test.py"
        spec = importlib.util.spec_from_file_location("private_migration", path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        async with engine.connect() as db:
            def inspect_and_downgrade(connection):
                columns = {item["name"]: item for item in inspect(connection).get_columns("private_reply_tests")}
                assert set(columns) == set(PrivateReplyTestRow.__table__.columns.keys())
                for column in PrivateReplyTestRow.__table__.columns:
                    assert columns[column.name]["nullable"] == column.nullable
                    assert columns[column.name]["type"].compile(dialect=connection.dialect) == column.type.compile(dialect=connection.dialect)
                actual_fk = {tuple(v["constrained_columns"]) for v in inspect(connection).get_foreign_keys("private_reply_tests")}
                assert actual_fk == {tuple(c.name for c in fk.columns) for fk in PrivateReplyTestRow.__table__.foreign_key_constraints}
                with Operations.context(MigrationContext.configure(connection)):
                    module.downgrade()
            with pytest.raises(RuntimeError, match="不得抹除"):
                await db.run_sync(inspect_and_downgrade)
            await db.rollback()
        async with sessions() as db:
            assert await db.scalar(select(PrivateReplyTestRow).where(PrivateReplyTestRow.tenant_id == tenant)) is not None
    finally:
        await engine.dispose()


async def test_private_marker_rls_blocks_other_tenant(runtime_database_url, db_url):
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    tenant, other = new_id("tn"), new_id("tn")
    admin = create_engine_from(db_url)
    sessions = async_sessionmaker(admin, expire_on_commit=False)
    try:
        await seed(sessions, tenant)
        await seed(sessions, other)
        restricted = create_engine_from(await runtime_database_url(tenant))
        try:
            async with restricted.begin() as db:
                rows = (await db.execute(select(PrivateReplyTestRow.tenant_id))).scalars().all()
                assert rows == [tenant]
                result = await db.execute(update(PrivateReplyTestRow).where(
                    PrivateReplyTestRow.tenant_id == other).values(created_by=new_id("emp")))
                assert result.rowcount == 0
            async with restricted.begin() as db:
                with pytest.raises(DBAPIError):
                    await db.execute(update(PrivateReplyTestRow).where(
                        PrivateReplyTestRow.tenant_id == tenant).values(tenant_id=other))
        finally:
            await restricted.dispose()
    finally:
        await admin.dispose()


async def test_private_marker_fresh_migration_roundtrip(db_url):
    import asyncio
    import os
    import subprocess
    import sys
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    # 唯一新库由测试拥有；不降级传入的共享夹具库或删除其中用途。
    name = "tradeos_private_migration_" + uuid4().hex
    admin = create_engine_from(db_url)
    target = make_url(str(db_url)).set(database=name).render_as_string(False)
    created = False
    try:
        async with admin.connect() as db:
            db = await db.execution_options(isolation_level="AUTOCOMMIT")
            await db.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        for action, revision in [("upgrade", "head"), ("downgrade", "0076"), ("upgrade", "head")]:
            result = await asyncio.to_thread(subprocess.run,
                [sys.executable, "scripts/run_alembic.py", action, revision],
                cwd=Path(__file__).parents[2],
                env={**os.environ, "DATABASE_URL": target, "PYTHON_DOTENV_DISABLED": "1"},
                capture_output=True, check=False, timeout=120)
            assert result.returncode == 0, f"private marker {action} failed"
        from tests.runtime_database_fixtures import runtime_database_scope
        async with runtime_database_scope(target) as provision:
            # 原启动检查同时验证FORCE RLS、双策略、表集合和真实角色授权。
            await provision(new_id("tn"))
    finally:
        try:
            if created:
                async with admin.connect() as db:
                    db = await db.execution_options(isolation_level="AUTOCOMMIT")
                    await db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()
