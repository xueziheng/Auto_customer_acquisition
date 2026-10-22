"""真实往返、追加约束和固定租户角色隔离。"""
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

from domains.conversations.schemas import ReplyWorkStatus
from infra.db.tables import ConversationReplyWorkUpdateRow
from infra.db.tenant_security import provision_tenant_role, tenant_database_role
from shared.schemas.identifiers import new_id
from tests.integration.test_reply_review_migration import migrate
from tests.integration.test_reply_work_handling import owned_work


async def test_handling_migration_preserves_history_and_enforces_tenant_role(integration_engine):
    name = "handling_migration_" + uuid4().hex
    async with integration_engine.connect() as db:
        db = await db.execution_options(isolation_level="AUTOCOMMIT")
        await db.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(integration_engine.url.set(database=name), hide_parameters=True)
    roles = []
    try:
        for action, target in [("upgrade", "0084"), ("downgrade", "0083"), ("upgrade", "0084")]:
            assert migrate(engine.url, action, target) == 0
        async with engine.begin() as db:
            await db.execute(text("CREATE SCHEMA handling_orm"))
            ddl = str(CreateTable(ConversationReplyWorkUpdateRow.__table__).compile(dialect=engine.dialect))
            await db.execute(text(ddl.replace(
                "CREATE TABLE conversation_reply_work_updates",
                "CREATE TABLE handling_orm.conversation_reply_work_updates", 1,
            )))
            async def shape(schema):
                return await db.run_sync(lambda c: (
                    [(r["name"], str(r["type"]), r["nullable"]) for r in inspect(c).get_columns(
                        "conversation_reply_work_updates", schema=schema)],
                    {r["name"]: r["sqltext"] for r in inspect(c).get_check_constraints(
                        "conversation_reply_work_updates", schema=schema)},
                ))
            assert await shape("public") == await shape("handling_orm")
            await db.execute(text("DROP SCHEMA handling_orm CASCADE"))
        _, tenant, actor, service, work = await owned_work(engine)
        await service.handle_reply_work(tenant, work.action_id, actor=actor,
            expected_status=ReplyWorkStatus.PENDING, status=ReplyWorkStatus.COMPLETED,
            disposition="handled", note="模拟验收处理，不是采购确认。", request_id="migration-handled")
        with pytest.raises(DBAPIError):
            async with engine.begin() as db:
                await db.execute(update(ConversationReplyWorkUpdateRow).where(
                    ConversationReplyWorkUpdateRow.tenant_id == tenant,
                ).values(note="覆盖历史"))
        assert migrate(engine.url, "downgrade", "0083") != 0
        for tenant_id, expected in [(tenant, 1), (new_id("tn"), 0)]:
            password = SecretStr(uuid4().hex + uuid4().hex)
            role = tenant_database_role(tenant_id)
            async with engine.begin() as db:
                await provision_tenant_role(db, tenant_id, password, create_only=True)
            roles.append(role)
            runtime = create_async_engine(engine.url.set(username=role, password=password.get_secret_value()))
            try:
                async with runtime.connect() as db:
                    records = (await db.execute(select(ConversationReplyWorkUpdateRow).where(
                        ConversationReplyWorkUpdateRow.tenant_id == tenant,
                    ))).all()
                    assert len(records) == expected
            finally:
                await runtime.dispose()
    finally:
        for role in roles:
            async with engine.begin() as db:
                await db.execute(text(f'DROP OWNED BY "{role}"'))
                await db.execute(text(f'DROP ROLE "{role}"'))
        await engine.dispose()
        async with integration_engine.connect() as db:
            db = await db.execution_options(isolation_level="AUTOCOMMIT")
            await db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
