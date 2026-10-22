"""0086空库往返与非空保护。"""
from sqlalchemy import inspect, text

from domains.conversations.business_mail_followup import BusinessMailHandlingCommand
from infra.db.business_mail_followup_tables import (
    BusinessMailFollowupEventRow,
    BusinessMailFollowupRow,
    BusinessMailReplyDraftRow,
)
from tests.integration.test_business_mail_followup import prepared
from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- pytest fixture 注册
)


async def test_followup_migration_roundtrip(unit_engine):
    assert migrate(unit_engine,'upgrade','0086')==0
    assert migrate(unit_engine,'downgrade','0085')==0
    assert migrate(unit_engine,'upgrade','0086')==0
    async with unit_engine.connect() as conn:
        for model in (BusinessMailFollowupRow,BusinessMailReplyDraftRow,BusinessMailFollowupEventRow):
            columns=await conn.run_sync(lambda c,m=model:inspect(c).get_columns(m.__tablename__))
            assert {c['name'] for c in columns}==set(model.__table__.columns.keys())
    assert migrate(unit_engine,"upgrade","head")==0
    _,actor,_,_,message,repo=await prepared(unit_engine)
    await repo.apply(actor,message,BusinessMailHandlingCommand(expected_revision=0,status='completed',note='处理'),idempotency_key='migration-test')
    assert migrate(unit_engine,'downgrade','0086')==0
    assert migrate(unit_engine,'downgrade','0085')!=0
    async with unit_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM business_mail_followup_events WHERE tenant_id=:tenant AND message_id=:message"), {"tenant": actor.tenant_id, "message": message}) == 1
