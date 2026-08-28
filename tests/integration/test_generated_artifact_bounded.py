"""真实PG Generated metadata+真实S3有限reader+计数SDK流，不宣称live S3。"""

# ruff: noqa: PLC0414 -- 显式复用隔离数据库fixture
import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from artifact_store.errors import (
    ArtifactBoundedReadUnavailable,
    ArtifactIntegrityError,
    ArtifactReadLimitExceeded,
)
from artifact_store.service_impl import GeneratedArtifactStoreImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from shared.schemas.identifiers import new_id
from tests.integration.test_need_units import unit_engine as unit_engine
from tests.integration.test_quote_pdf_artifacts import NOW, MemoryBlobs, pdf_args
from tests.unit.test_quote_evidence_s3 import FiniteBody, case


def bounded_store(engine, reader):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return GeneratedArtifactStoreImpl(
        lambda tenant: SqlAlchemyArtifactUnitOfWork(sessions, tenant),
        MemoryBlobs(),
        10000,
        lambda: NOW,
        new_id,
        bounded_transport=reader,
    )


@pytest.mark.parametrize(
    "actual,code",
    [(b"x" * 1000, "limit"), (b"short", "integrity"), (b"x" * 21, "integrity")],
)
async def test_actual_object_read_is_bounded(unit_engine, monkeypatch, actual, code):
    body = FiniteBody(actual)
    reader, _, client, _ = case(monkeypatch, body, chunk_bytes=3)
    store = bounded_store(unit_engine, reader)
    args = pdf_args()
    args["content"] = b"controlled-pdf-bytes!"
    meta = await store.put(**args)
    assert meta.size_bytes == 21 and meta.size_bytes < 100
    with pytest.raises(
        ArtifactReadLimitExceeded if code == "limit" else ArtifactIntegrityError
    ):
        await store.get_bounded(meta.tenant_id, meta.artifact_id, maximum_bytes=100)
    assert len(actual) - len(body.data) <= meta.size_bytes + 1
    assert body.closed and client.closed


async def test_metadata_limit_and_missing_bounded_dependency_have_zero_object_io(
    unit_engine, monkeypatch
):
    reader, secrets, _, configs = case(monkeypatch)
    store = bounded_store(unit_engine, reader)
    meta = await store.put(**pdf_args())
    with pytest.raises(ArtifactReadLimitExceeded):
        await store.get_bounded(meta.tenant_id, meta.artifact_id, maximum_bytes=1)
    assert secrets.calls == 0 and not configs
    without = bounded_store(unit_engine, None)
    with pytest.raises(ArtifactBoundedReadUnavailable):
        await without.get_bounded(meta.tenant_id, meta.artifact_id, maximum_bytes=100)


async def test_generated_bounded_success_and_cross_tenant_zero_io(
    unit_engine, monkeypatch
):
    from artifact_store.errors import ArtifactNotFoundError
    from infra.quote_document_store import GeneratedStoreDocumentAdapter

    args = pdf_args()
    reader, secrets, client, configs = case(
        monkeypatch, FiniteBody(args["content"]), chunk_bytes=3
    )
    store = bounded_store(unit_engine, reader)
    meta = await store.put(**args)
    with pytest.raises(ArtifactNotFoundError):
        await store.get_bounded(new_id("tn"), meta.artifact_id, maximum_bytes=100)
    assert secrets.calls == 0 and configs == []
    payload = await GeneratedStoreDocumentAdapter(store, store).get_bounded(
        meta.tenant_id, meta.artifact_id, maximum_bytes=100
    )
    assert (
        payload.content == args["content"]
        and payload.meta.artifact_hash == meta.content_hash
    )
    assert "controlled-pdf" not in repr(payload) and client.closed


async def test_generated_bounded_cancel_keeps_original_cancel_and_closes(
    unit_engine, monkeypatch
):
    body = FiniteBody(b"x" * 1000, delay=0.05)
    reader, _, client, _ = case(monkeypatch, body, chunk_bytes=3)
    store = bounded_store(unit_engine, reader)
    meta = await store.put(**pdf_args())
    task = asyncio.create_task(
        store.get_bounded(meta.tenant_id, meta.artifact_id, maximum_bytes=100)
    )
    async with asyncio.timeout(1):
        while not body.started.is_set():
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed and client.closed
