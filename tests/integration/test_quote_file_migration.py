"""0046真实隔离库的分支CHECK、往返及metadata表映射。"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.integration.test_quote_pdf_artifacts import (
    NOW,
    MemoryBlobs,
    pdf_args,
    store_for,
)


async def test_0046_empty_roundtrip_and_columns(unit_engine):
    async with unit_engine.connect() as conn:
        assert "quotation_files" in await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert migrate(unit_engine, "downgrade", "0045") == 0
    assert migrate(unit_engine, "upgrade", "head") == 0
    from infra.db.tables import Base

    async with unit_engine.connect() as conn:
        columns = await conn.run_sync(lambda c: inspect(c).get_columns("quotation_files"))
    assert {c["name"] for c in columns} == set(Base.metadata.tables["quotation_files"].columns.keys())


async def test_pdf_data_prevents_downgrade(unit_engine):
    store = store_for(unit_engine, MemoryBlobs())
    winner = await store.put(**pdf_args())
    assert migrate(unit_engine, "downgrade", "0045") != 0
    assert await store.get_meta(winner.tenant_id, winner.artifact_id) == winner


@pytest.mark.parametrize(("field", "value"), [
    ("kind", "email_draft"), ("mime_type", "text/plain"),
    ("subject_ref", "enr_00000000000000000000000000"),
    ("generated_by", "arbitrary"), ("idempotency_key", "arbitrary"),
])
async def test_db_rejects_cross_kind_pdf_bindings(unit_engine, field, value):
    from sqlalchemy import insert

    from infra.db.tables import GeneratedArtifactRow

    args = pdf_args()
    artifact_id = "art_00000000000000000000000000"
    values = {k: v for k, v in args.items() if k != "content"}
    values.update(kind="quote_pdf", artifact_id=artifact_id, content_hash="a" * 64,
        size_bytes=4, object_key=f"generated/{args['tenant_id']}/{artifact_id}",
        generated_at=NOW)
    values[field] = value
    async with unit_engine.begin() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(insert(GeneratedArtifactRow).values(**values))
