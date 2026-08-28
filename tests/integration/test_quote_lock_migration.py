"""0043只在隔离Postgres升级/降级，业务记录存在时必须拒绝降级。"""

from sqlalchemy import inspect

from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import unit_engine as unit_engine


async def test_empty_quote_lock_migration_roundtrip(unit_engine) -> None:
    async with unit_engine.connect() as connection:
        names = await connection.run_sync(lambda c: inspect(c).get_table_names())
    assert {
        "cost_scope_confirmations",
        "costing_quote_bases",
        "quote_creation_operations",
    } <= set(names), "缺少0043冻结表"
    assert migrate(unit_engine, "downgrade", "0042") == 0
    assert migrate(unit_engine, "upgrade", "head") == 0
