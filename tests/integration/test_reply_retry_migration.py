"""0081 空库往返与 ORM 一致。"""

from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from infra.db.reply_retry_tables import ReplyRetryRow
from tests.integration.test_reply_review_migration import migrate


async def test_reply_retry_migration_roundtrip(integration_engine):
    name = "reply_retry_" + uuid4().hex
    async with integration_engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(integration_engine.url.set(database=name))
    try:
        for action, target in [
            ("upgrade", "0081"),
            ("downgrade", "0080"),
            ("upgrade", "0081"),
        ]:
            assert migrate(engine.url, action, target) == 0
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda c: inspect(c).get_columns("reply_classification_retries")
            )
            assert {c["name"] for c in columns} == set(
                set(ReplyRetryRow.__table__.columns.keys()) - {"sequence"}
            )
            policies = (
                (
                    await conn.execute(
                        text(
                            "SELECT polname FROM pg_policy WHERE polrelid='reply_classification_retries'::regclass"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(policies) == {"tradeos_tenant_allow", "tradeos_tenant_guard"}
        assert migrate(engine.url, "upgrade", "head") == 0
        from tests.integration.test_reply_retry import seed

        _sessions, tenant, _run, message, actor, _config, repo = await seed(engine)
        await repo.request(tenant, message, actor, "migration-preserve")
        assert migrate(engine.url, "downgrade", "0080") != 0
        assert (await repo.read(tenant, message, actor)).state == "queued"
    finally:
        await engine.dispose()
