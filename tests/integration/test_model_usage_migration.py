"""模型费用账本升级往返与拒绝有数据的降级，使用隔离数据库。"""

from datetime import UTC, datetime

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker

from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_model_usage import identity, limits
from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)


async def test_model_migration_roundtrip_and_refuses_losing_usage(unit_engine):
    from infra.db.model_usage import SqlModelUsageRepository
    from infra.db.tables import Base

    assert migrate(unit_engine, "downgrade", "0060") == 0
    assert migrate(unit_engine, "upgrade", "head") == 0
    async with unit_engine.connect() as connection:
        for name in (
            "model_invocations",
            "model_quota_buckets",
            "model_slot_releases",
            "model_configuration_versions",
        ):
            columns = await connection.run_sync(
                lambda c, n=name: inspect(c).get_columns(n)
            )
            assert {c["name"] for c in columns} == set(
                Base.metadata.tables[name].columns.keys()
            )
    repo = SqlModelUsageRepository(
        async_sessionmaker(unit_engine, expire_on_commit=False)
    )
    tenant = TenantId(new_id("tn"))
    claim = await repo.reserve(
        identity(tenant), "a" * 64, limits(), datetime.now(UTC), model="test-model"
    )
    assert migrate(unit_engine, "downgrade", "0060") != 0
    assert (await repo.get(tenant, claim.invocation_id)).state == "reserved"
