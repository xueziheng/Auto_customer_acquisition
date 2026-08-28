"""0042隔离数据库往返与有证据降级拒绝。"""

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    inspect,
    text,
)

from tests.integration import test_need_units as support

unit_db_case = support.unit_db_case
unit_engine = support.unit_engine
migrate = support.migrate


async def test_0042_has_three_nullable_columns_and_confirmation_table(
    unit_engine,
) -> None:
    async with unit_engine.connect() as connection:
        tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
        columns = await connection.run_sync(
            lambda c: inspect(c).get_columns("validated_needs")
        )
    assert "need_unit_confirmations" in tables
    by_name = {c["name"]: c for c in columns}
    assert all(
        by_name[n]["nullable"]
        for n in ("unit", "unit_quantity_fact_hash", "unit_confirmation_id")
    )


async def test_confirmation_schema_matches_orm(unit_engine) -> None:
    """新增表列/PK/唯一键/FK/CHECK与ORM完全对应；不是仅检查表名。"""
    from infra.db.tables import NeedUnitConfirmationRow

    table = NeedUnitConfirmationRow.__table__
    async with unit_engine.connect() as connection:
        actual = await connection.run_sync(
            lambda c: {
                "columns": {
                    item["name"]: (
                        str(item["type"].compile(dialect=unit_engine.dialect)),
                        item["nullable"],
                    )
                    for item in inspect(c).get_columns(table.name)
                },
                "pk": inspect(c).get_pk_constraint(table.name)["constrained_columns"],
                "uniques": {
                    item["name"]: item["column_names"]
                    for item in inspect(c).get_unique_constraints(table.name)
                },
                "checks": {
                    item["name"]
                    for item in inspect(c).get_check_constraints(table.name)
                },
                "fks": {
                    item["name"]: (
                        item["constrained_columns"],
                        item["referred_table"],
                        item["referred_columns"],
                    )
                    for item in inspect(c).get_foreign_keys(table.name)
                },
            }
        )
    assert actual["columns"] == {
        name: (str(col.type.compile(dialect=unit_engine.dialect)), col.nullable)
        for name, col in table.columns.items()
    }
    assert actual["pk"] == [col.name for col in table.primary_key.columns]
    assert actual["uniques"] == {
        c.name: [col.name for col in c.columns]
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert actual["checks"] == {
        c.name for c in table.constraints if isinstance(c, CheckConstraint)
    }
    assert actual["fks"] == {
        c.name: (
            [col.name for col in c.columns],
            c.referred_table.name,
            [element.column.name for element in c.elements],
        )
        for c in table.constraints
        if isinstance(c, ForeignKeyConstraint)
    }


async def test_downgrade_with_confirmation_refuses_and_preserves_receipt(
    unit_db_case,
) -> None:
    c = unit_db_case
    receipt = await c.confirm()
    assert migrate(c.engine, "downgrade", "0041") != 0
    assert await c.confirm() == receipt
    async with c.engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "0042"


async def test_empty_unit_roundtrip_preserves_old_need(unit_db_case) -> None:
    c = unit_db_case
    try:
        assert migrate(c.engine, "downgrade", "0041") == 0
        async with c.engine.connect() as connection:
            before = (
                await connection.execute(
                    text(
                        "SELECT quantity FROM validated_needs "
                        "WHERE tenant_id=:tenant AND need_id=:need"
                    ),
                    {"tenant": c.tenant, "need": c.need_id},
                )
            ).scalar_one()
        assert migrate(c.engine, "upgrade", "0042") == 0
        async with c.engine.connect() as connection:
            result = (
                await connection.execute(
                    text(
                        "SELECT quantity,unit,unit_quantity_fact_hash,unit_confirmation_id "
                        "FROM validated_needs WHERE tenant_id=:tenant AND need_id=:need"
                    ),
                    {"tenant": c.tenant, "need": c.need_id},
                )
            ).one()
        assert result == (before, None, None, None)
        assert migrate(c.engine, "downgrade", "0041") == 0
        assert migrate(c.engine, "upgrade", "0042") == 0
    finally:
        assert migrate(c.engine, "upgrade", "head") == 0
