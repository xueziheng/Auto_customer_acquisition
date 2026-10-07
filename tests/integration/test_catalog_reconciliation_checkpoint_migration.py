"""0058 Catalog reconciliation checkpoint schema 与 CAS 的真实 PostgreSQL 契约。"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from infra.db.catalog_reconciliation_checkpoints import (
    PostgresCatalogReconciliationCheckpointStore,
)
from shared.errors import TenantIsolationViolation, TransientError
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- 每例独立真实 PostgreSQL
)

_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _alembic(db_url: str, *args: str, succeeds: bool = True) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *args],
        cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is succeeds, "0058 migration 结果不符合预期"


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().replace('"', "")).strip()


async def test_0058_empty_upgrade_downgrade_upgrade_has_exact_orm_parity(
    unit_engine: AsyncEngine,
) -> None:
    from infra.db.session import create_engine_from
    from infra.db.tables import CatalogReconciliationCheckpointRow

    db_url = unit_engine.url.render_as_string(False)
    try:
        _alembic(db_url, "downgrade", "0057")
        engine = create_engine_from(db_url)
        try:
            async with engine.connect() as connection:
                names = set(
                    await connection.run_sync(
                        lambda sync: inspect(sync).get_table_names()
                    )
                )
            assert "catalog_reconciliation_checkpoints" not in names
        finally:
            await engine.dispose()

        _alembic(db_url, "upgrade", "0058")
        engine = create_engine_from(db_url)
        try:
            async with engine.connect() as connection:
                contract = await connection.run_sync(
                    lambda sync: {
                        "columns": {
                            str(item["name"]): bool(item["nullable"])
                            for item in inspect(sync).get_columns(
                                "catalog_reconciliation_checkpoints"
                            )
                        },
                        "checks": {
                            str(item["name"]): _normalize(str(item["sqltext"]))
                            for item in inspect(sync).get_check_constraints(
                                "catalog_reconciliation_checkpoints"
                            )
                        },
                        "pk": tuple(
                            str(value)
                            for value in inspect(sync)
                            .get_pk_constraint(
                                "catalog_reconciliation_checkpoints"
                            )["constrained_columns"]
                        ),
                    }
                )
            assert contract["columns"] == {
                "tenant_id": False,
                "stream": False,
                "position_at": True,
                "entity_id": True,
                "version": False,
            }
            assert contract["pk"] == ("tenant_id", "stream")
            assert set(contract["checks"]) == {
                "ck_catalog_reconciliation_checkpoint_scope",
                "ck_catalog_reconciliation_checkpoint_position",
                "ck_catalog_reconciliation_checkpoint_entity",
            }
            assert "version >= 1" in contract["checks"][
                "ck_catalog_reconciliation_checkpoint_position"
            ]
            assert "pending_policies" in contract["checks"][
                "ck_catalog_reconciliation_checkpoint_scope"
            ]
            assert "^ncl_" in contract["checks"][
                "ck_catalog_reconciliation_checkpoint_entity"
            ]
            assert set(
                CatalogReconciliationCheckpointRow.__table__.columns.keys()
            ) == set(contract["columns"])
        finally:
            await engine.dispose()

        _alembic(db_url, "downgrade", "0057")
        _alembic(db_url, "upgrade", "0058")
    finally:
        _alembic(db_url, "upgrade", "head")


async def test_0058_nonempty_downgrade_refuses_checkpoint_loss(
    unit_engine: AsyncEngine,
) -> None:
    from infra.db.session import create_engine_from

    db_url = unit_engine.url.render_as_string(False)
    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO catalog_reconciliation_checkpoints "
                    "(tenant_id,stream,position_at,entity_id,version) VALUES "
                    "(:tenant,'catalog_clusters',NULL,NULL,1)"
                ),
                {"tenant": new_id("tn")},
            )
        _alembic(db_url, "downgrade", "0057", succeeds=False)
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM catalog_reconciliation_checkpoints")
            )
    finally:
        await engine.dispose()
        _alembic(db_url, "upgrade", "head")


@pytest.mark.asyncio
async def test_checkpoint_store_cas_conflict_reset_and_tenant_isolation(
    unit_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    store = PostgresCatalogReconciliationCheckpointStore(factory, tenant)
    current = await store.load(tenant, "pending_policies")
    assert (current.version, current.position_at, current.entity_id) == (0, None, None)

    outcomes = await asyncio.gather(
        store.compare_and_set(
            current,
            next_position_at=_NOW,
            next_entity_id="cpv_checkpoint_a",
        ),
        store.compare_and_set(
            current,
            next_position_at=_NOW,
            next_entity_id="cpv_checkpoint_b",
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    assert sum(isinstance(value, TransientError) for value in outcomes) == 1

    persisted = await store.load(tenant, "pending_policies")
    assert persisted.version == 1
    assert persisted.position_at == _NOW
    assert persisted.entity_id in {"cpv_checkpoint_a", "cpv_checkpoint_b"}
    with pytest.raises(TransientError):
        await store.compare_and_set(
            current,
            next_position_at=_NOW,
            next_entity_id="cpv_checkpoint_stale",
        )
    reset = await store.compare_and_set(
        persisted,
        next_position_at=None,
        next_entity_id=None,
    )
    assert (reset.version, reset.position_at, reset.entity_id) == (2, None, None)
    assert await store.load(tenant, "pending_policies") == reset

    with pytest.raises(TenantIsolationViolation):
        await store.load(other_tenant, "pending_policies")
