"""免费套餐资格迁移保守保留历史未知状态，并支持隔离数据库往返。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from infra.db.session import create_engine_from
from infra.db.tables import SearchQuotaAccountRow
from tests.integration.conftest import RedactedUrl
from tests.integration.test_migrations import _run_alembic


async def test_included_credits_migration_keeps_history_closed_and_roundtrips(
    integration_engine: AsyncEngine,
) -> None:
    database_name = "test_included_credits_" + uuid4().hex
    async with integration_engine.connect() as connection:
        admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{database_name}"'))
    isolated_url = RedactedUrl(
        integration_engine.url.set(database=database_name).render_as_string(False)
    )
    engine = create_engine_from(isolated_url)
    tenant = "tn_included_credits_migration"
    try:
        _run_alembic(isolated_url, "upgrade", "0067")
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO search_quota_accounts "
                "(tenant_id,provider,ceiling,reservations,cost_status,usage_limit,usage_used) "
                "VALUES (:tenant,'tavily',2,1,'unknown',1000,998)"
            ), {"tenant": tenant})

        _run_alembic(isolated_url, "upgrade", "head")
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {item["name"]: item for item in inspect(sync).get_columns(
                    "search_quota_accounts"
                )}
            )
            assert "included_credits_free" in columns
            assert columns["included_credits_free"]["nullable"] is False
            assert columns["included_credits_free"]["default"] == "false"
            assert set(columns) == set(SearchQuotaAccountRow.__table__.columns.keys())
            legacy = (await connection.execute(text(
                "SELECT included_credits_free,cost_status,paygo_enabled,ceiling,reservations "
                "FROM search_quota_accounts WHERE tenant_id=:tenant"
            ), {"tenant": tenant})).one()
            assert tuple(legacy) == (False, "unknown", None, 2, 1)

        async with engine.begin() as connection:
            await connection.execute(text(
                "UPDATE search_quota_accounts SET included_credits_free=true "
                "WHERE tenant_id=:tenant"
            ), {"tenant": tenant})
        for assignment in (
            "cost_status='paid'", "paygo_enabled=true", "usage_limit=NULL", "usage_used=NULL",
        ):
            async with engine.connect() as connection:
                with pytest.raises(IntegrityError):
                    await connection.execute(text(
                        f"UPDATE search_quota_accounts SET {assignment} WHERE tenant_id=:tenant"
                    ), {"tenant": tenant})
                await connection.rollback()

        _run_alembic(isolated_url, "downgrade", "0067")
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_columns(
                    "search_quota_accounts"
                )}
            )
            assert "included_credits_free" not in columns
        _run_alembic(isolated_url, "upgrade", "head")
        async with engine.connect() as connection:
            restored = (await connection.execute(text(
                "SELECT included_credits_free,cost_status,paygo_enabled,ceiling,reservations "
                "FROM search_quota_accounts WHERE tenant_id=:tenant"
            ), {"tenant": tenant})).one()
            assert tuple(restored) == (False, "unknown", None, 2, 1)
    finally:
        await engine.dispose()
        async with integration_engine.connect() as connection:
            admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{database_name}"'))
