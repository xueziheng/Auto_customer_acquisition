"""会话迁移不影响原有 Run，且拒绝丢失会话历史的降级。"""

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.integration.test_assistant import actor, service
from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.integration.test_search_quota import _workflow_run


async def test_assistant_roundtrip_preserves_run_and_guards_nonempty(unit_engine):
    from infra.db.tables import AgentSessionRow, AgentTurnRow, WorkflowRunRow

    user = actor()
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    run = await _workflow_run(factory, user.tenant_id)
    assert migrate(unit_engine, "downgrade", "0061") == 0
    assert migrate(unit_engine, "upgrade", "head") == 0
    async with unit_engine.connect() as conn:
        for model in (AgentSessionRow, AgentTurnRow):
            columns = await conn.run_sync(
                lambda c, n=model.__tablename__: inspect(c).get_columns(n)
            )
            assert {c["name"] for c in columns} == set(model.__table__.columns.keys())
    async with factory() as db:
        assert (
            await db.scalar(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == user.tenant_id,
                    WorkflowRunRow.run_id == run,
                )
            )
            is not None
        )
    await service(unit_engine).create_session(user)
    assert migrate(unit_engine, "downgrade", "0061") != 0
