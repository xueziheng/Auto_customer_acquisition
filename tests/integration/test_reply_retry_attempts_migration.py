"""0083 往返和旧证据保留；已有第二次尝试时不允许降级。"""

from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from infra.db.reply_retry_tables import ReplyRetryRow
from tests.integration.test_reply_retry_failed import failed_retry
from tests.integration.test_reply_review_migration import migrate
from workflows.reply_qualification.retry import ReplyFailedRetryCommand


async def test_attempt_migration_roundtrip_preserves_failure(integration_engine):
    name = "reply_attempts_" + uuid4().hex
    async with integration_engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(integration_engine.url.set(database=name))
    try:
        for action, target in [
            ("upgrade", "0083"),
            ("downgrade", "0082"),
            ("upgrade", "0083"),
        ]:
            assert migrate(engine.url, action, target) == 0
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda c: inspect(c).get_columns("reply_classification_retries")
            )
            assert {c["name"] for c in columns} == set(
                ReplyRetryRow.__table__.columns.keys()
            )
        sessions, tenant, _run, message, actor, config, repo = await failed_retry(
            engine
        )
        assert migrate(engine.url, "downgrade", "0082") == 0
        assert migrate(engine.url, "upgrade", "0083") == 0
        async with sessions() as db:
            row = await db.scalar(
                select(ReplyRetryRow).where(ReplyRetryRow.tenant_id == tenant)
            )
            assert (row.sequence, row.state, row.input_bytes, row.output_tokens) == (
                1,
                "failed",
                21425,
                28,
            )
        await repo.retry_failed(
            tenant,
            message,
            actor,
            "migration-stopped",
            ReplyFailedRetryCommand(expected_sequence=1, acknowledge_cost=True),
        )
        assert migrate(engine.url, "downgrade", "0082") != 0
        with pytest.raises(DBAPIError):
            async with sessions.begin() as db:
                row = await db.scalar(
                    select(ReplyRetryRow).where(
                        ReplyRetryRow.tenant_id == tenant, ReplyRetryRow.sequence == 1
                    )
                )
                row.sequence = 99
        assert (
            await repo.retry_identity(tenant, message, config.version)
        ).sequence == 2
    finally:
        await engine.dispose()
