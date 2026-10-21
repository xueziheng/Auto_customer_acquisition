"""独立空数据库迁移往返；不删除账本数据来强制降级。"""

from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialEventRow, QQTrialRow
from tests.integration.test_reply_review_migration import migrate


async def test_empty_roundtrip_and_schema_matches_orm(integration_engine):
    name = "qq_trial_migration_" + uuid4().hex
    async with integration_engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(integration_engine.url.set(database=name))
    try:
        for action, target in [
            ("upgrade", "0078"),
            ("upgrade", "0079"),
            ("downgrade", "0078"),
            ("upgrade", "0079"),
        ]:
            assert migrate(engine.url, action, target) == 0
        async with engine.connect() as conn:
            tx = await conn.begin()
            await conn.execute(text("CREATE SCHEMA qq_trial_orm"))
            for row in (QQSentAliasRow, QQTrialRow, QQTrialEventRow):
                table = row.__tablename__
                ddl = str(CreateTable(row.__table__).compile(dialect=engine.dialect))
                ddl = ddl.replace(
                    f"CREATE TABLE {table}", f"CREATE TABLE qq_trial_orm.{table}", 1
                )
                await conn.execute(text(ddl))
                real = await conn.run_sync(
                    lambda c, table=table: inspect(c).get_columns(table)
                )
                orm = await conn.run_sync(
                    lambda c, table=table: inspect(c).get_columns(
                        table, schema="qq_trial_orm"
                    )
                )
                assert {(r["name"], str(r["type"]), r["nullable"]) for r in real} == {
                    (r["name"], str(r["type"]), r["nullable"]) for r in orm
                }
                real = await conn.run_sync(
                    lambda c, table=table: inspect(c).get_check_constraints(table)
                )
                orm = await conn.run_sync(
                    lambda c, table=table: inspect(c).get_check_constraints(
                        table, schema="qq_trial_orm"
                    )
                )
                assert {r["name"]: r["sqltext"] for r in real} == {
                    r["name"]: r["sqltext"] for r in orm
                }
            await tx.rollback()
    finally:
        await engine.dispose()
        # 保留这次独立库用于离线审查；不修改调用方数据库。
