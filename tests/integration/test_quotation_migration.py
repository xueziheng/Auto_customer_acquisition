"""0044真实SQL不可变和tenant复合约束；隔离数据库往返。"""

from sqlalchemy import inspect

from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- pytest跨文件fixture显式导出
)

TABLES = {
    "quotation_issuers",
    "quotations",
    "quotation_lines",
    "quotation_evidence_refs",
    "quotation_state_events",
    "quotation_approval_bindings",
    "quotation_send_receipts",
}


async def test_0044_upgrade_downgrade_upgrade(unit_engine):
    async with unit_engine.connect() as conn:
        names = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    assert TABLES <= names, "0044不可变报价表尚未建立"
    assert migrate(unit_engine, "downgrade", "0043") == 0
    async with unit_engine.connect() as conn:
        assert TABLES.isdisjoint(
            await conn.run_sync(lambda c: inspect(c).get_table_names())
        )
    assert migrate(unit_engine, "upgrade", "head") == 0
    from infra.db.tables import Base

    async with unit_engine.connect() as conn:
        for name in TABLES:
            columns = await conn.run_sync(
                lambda c, table_name=name: inspect(c).get_columns(table_name)
            )
            assert {c["name"] for c in columns} == set(
                Base.metadata.tables[name].columns.keys()
            )
            assert "tenant_id" in {c["name"] for c in columns}
