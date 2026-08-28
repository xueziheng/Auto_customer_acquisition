"""0045隔离数据库往返与namespace约束，不修改生产库。"""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)


async def test_0045_roundtrip_and_orm_columns(unit_engine):
    async with unit_engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert "quotation_approval_receipts" in names, "缺少0045成功receipt表"
    assert migrate(unit_engine, "downgrade", "0044") == 0
    assert migrate(unit_engine, "upgrade", "head") == 0
    from infra.db.tables import Base

    async with unit_engine.connect() as conn:
        for name in (
            "approval_packages",
            "quotation_approval_bindings",
            "quotation_approval_receipts",
        ):
            columns = await conn.run_sync(
                lambda c, table=name: inspect(c).get_columns(table)
            )
            assert {item["name"] for item in columns} == set(
                Base.metadata.tables[name].columns.keys()
            )


@pytest.mark.parametrize("marker", ["quote:invalid", " QUOTE:invalid", ""])
async def test_marker_cannot_be_inserted_as_legacy(unit_engine, marker):
    from datetime import timedelta

    from tests.integration.test_need_units import NOW

    async with unit_engine.begin() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(
                text("""INSERT INTO approval_packages
                (tenant_id,approval_id,approval_type,title,proposed_change,reason,blast_radius,
                 created_at,expires_at,state,evidence_refs,change_set_ref)
                VALUES ('t','apr_test','quote_send','test',CAST(:payload AS jsonb),'test',
                 '{}',:created,:expiry,'pending','[]',:marker)"""),
                {
                    "payload": '{"schema_version":"quote-approval-v1"}'
                    if not marker
                    else "{}",
                    "marker": marker,
                    "created": NOW,
                    "expiry": NOW + timedelta(days=1),
                },
            )
