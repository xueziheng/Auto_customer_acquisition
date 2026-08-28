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
    from infra.db.tables import Base

    def compare(connection):
        inspector = inspect(connection)
        for name in (
            "cost_scope_confirmations",
            "costing_quote_bases",
            "quote_creation_operations",
        ):
            expected = Base.metadata.tables[name]
            columns = inspector.get_columns(name)
            assert {c["name"]: c["nullable"] for c in columns} == {
                c.name: c.nullable for c in expected.columns
            }
            assert {c["name"] for c in inspector.get_check_constraints(name)} == {
                c.name
                for c in expected.constraints
                if type(c).__name__ == "CheckConstraint"
            }
            actual = inspector.get_foreign_keys(name)
            assert {tuple(c["constrained_columns"]) for c in actual} == {
                tuple(c.columns.keys()) for c in expected.foreign_key_constraints
            }
            for c in actual:
                if c["name"] in {
                    "fk_costing_quote_bases_operation_id",
                    "fk_quote_creation_operations_basis_id",
                }:
                    assert (
                        c["options"]["deferrable"] is True
                        and c["options"]["initially"] == "DEFERRED"
                    )
        indexes = inspector.get_indexes("quote_creation_operations")
        pending = next(
            i for i in indexes if i["name"] == "uq_quote_creation_operations_pending"
        )
        assert (
            pending["unique"]
            and "frozen" in pending["dialect_options"]["postgresql_where"]
        )
        assert not any(
            c["column_names"] == ["tenant_id", "cost_sheet_id"]
            for c in inspector.get_unique_constraints("quote_creation_operations")
        )

    async with unit_engine.connect() as connection:
        await connection.run_sync(compare)
