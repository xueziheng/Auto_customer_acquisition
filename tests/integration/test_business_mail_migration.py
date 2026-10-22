"""0085真实Postgres往返、ORM契约及不可有损回退。"""

from sqlalchemy import inspect

from infra.db.business_mail_tables import (
    BusinessMailboxRow,
    BusinessMailClassificationRow,
    BusinessMailReceiptRow,
    BusinessMailRow,
)
from tests.integration.test_business_mail import setup_mail
from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)


async def test_business_mail_migration_roundtrip_and_refuses_data_loss(unit_engine):
    assert migrate(unit_engine, "downgrade", "0084") == 0
    assert migrate(unit_engine, "upgrade", "0085") == 0
    async with unit_engine.connect() as conn:
        for model in (
            BusinessMailboxRow,
            BusinessMailRow,
            BusinessMailReceiptRow,
            BusinessMailClassificationRow,
        ):
            columns = await conn.run_sync(
                lambda c, m=model: inspect(c).get_columns(m.__tablename__)
            )
            assert {c["name"] for c in columns} == set(model.__table__.columns.keys())
    assert migrate(unit_engine, "downgrade", "0084") == 0
    assert migrate(unit_engine, "upgrade", "0085") == 0
    await setup_mail(unit_engine)
    assert migrate(unit_engine, "downgrade", "0084") != 0
