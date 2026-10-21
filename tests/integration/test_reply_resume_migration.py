"""0082 往返、ORM一致、旧期限保留与拒绝证据丢失。"""

from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from infra.db.reply_resume_tables import ReplyResumeRow
from infra.db.reply_retry_tables import ReplyRetryRow
from tests.integration.test_reply_retry_resume import expired
from tests.integration.test_reply_review_migration import migrate


async def test_resume_migration_roundtrip_and_preserve_legacy(integration_engine):
    name = "reply_resume_" + uuid4().hex
    async with integration_engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(integration_engine.url.set(database=name))
    try:
        for action, target in [
            ("upgrade", "0082"),
            ("downgrade", "0081"),
            ("upgrade", "0082"),
        ]:
            assert migrate(engine.url, action, target) == 0
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda c: inspect(c).get_columns("reply_classification_resumptions")
            )
            assert {c["name"] for c in columns} == set(
                set(ReplyResumeRow.__table__.columns.keys()) - {"retry_sequence"}
            )
            policies = (
                (
                    await conn.execute(
                        text(
                            "SELECT polname FROM pg_policy WHERE polrelid='reply_classification_resumptions'::regclass"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(policies) == {"tradeos_tenant_allow", "tradeos_tenant_guard"}
            assert await conn.scalar(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='reply_classification_resumptions'::regclass"
                )
            )
        assert migrate(engine.url, "upgrade", "head") == 0
        sessions, tenant, _run, message, actor, _config, repo = await expired(engine)
        async with sessions() as db:
            old = (
                await db.scalar(
                    select(ReplyRetryRow).where(ReplyRetryRow.tenant_id == tenant)
                )
            ).expires_at
        assert migrate(engine.url, "downgrade", "0081") == 0
        assert migrate(engine.url, "upgrade", "head") == 0
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(ReplyRetryRow).where(ReplyRetryRow.tenant_id == tenant)
                )
            ).expires_at == old
        await repo.resume(tenant, message, actor, "migration-resume")
        assert migrate(engine.url, "downgrade", "0081") != 0
        assert (await repo.read(tenant, message, actor)).resumed
    finally:
        await engine.dispose()
        async with integration_engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP DATABASE "{name}"'))
