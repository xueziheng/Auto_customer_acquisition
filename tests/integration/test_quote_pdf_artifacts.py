"""真实隔离PG metadata与受控bytes；不运行PDF作者或外部Provider。"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.errors import ArtifactConflictError
from artifact_store.service_impl import GeneratedArtifactStoreImpl
from artifact_store.store import GeneratedArtifactKind
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from shared.schemas.identifiers import new_id

NOW = datetime(2026, 8, 28, tzinfo=UTC)


class MemoryBlobs:
    def __init__(self, barrier: asyncio.Barrier | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.gets = self.puts = self.deletes = 0
        self.barrier = barrier

    async def put(self, object_key: str, content: bytes) -> None:
        self.puts += 1
        self.objects[object_key] = content
        if self.barrier is not None:
            await self.barrier.wait()

    async def get(self, object_key: str) -> bytes:
        self.gets += 1
        return self.objects[object_key]

    async def delete(self, object_key: str) -> None:
        self.deletes += 1
        self.objects.pop(object_key, None)


def pdf_args() -> dict:
    quote = new_id("quo")
    return {"tenant_id": new_id("tn"), "kind": GeneratedArtifactKind.QUOTE_PDF,
        "content": b"controlled-pdf-bytes", "mime_type": "application/pdf",
        "workflow_run_id": new_id("run"), "subject_ref": quote, "sequence_number": 1,
        "idempotency_key": f"{quote}:1:quote_pdf:quote_pdf_v1", "generated_by": "quote_pdf_v1"}


def store_for(engine, blobs: MemoryBlobs, *, fault: str | None = None):
    class CloseFailedSession(AsyncSession):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("private close detail")

    sessions = async_sessionmaker(engine, expire_on_commit=False,
        class_=CloseFailedSession if fault == "close_failed" else AsyncSession)

    @asynccontextmanager
    async def factory(tenant):
        async with SqlAlchemyArtifactUnitOfWork(sessions, tenant) as uow:
            yield uow
            if fault == "before_commit":
                raise RuntimeError("private storage detail")
        # 此处已真实commit且close，模拟commit成功后的响应丢失。
        if fault == "after_commit":
            raise RuntimeError("private storage detail")
        if fault == "cancel_after_commit":
            raise asyncio.CancelledError()

    return GeneratedArtifactStoreImpl(factory, blobs, 10000, lambda: NOW, new_id)


@pytest.mark.parametrize("fault", ["after_commit", "cancel_after_commit", "before_commit", "close_failed"])
async def test_pdf_unknown_commit_preserves_bytes_and_original_key_recovers(integration_engine, fault):
    blobs, args = MemoryBlobs(), pdf_args()
    faulty = store_for(integration_engine, blobs, fault=fault)
    with pytest.raises(BaseException) as caught:
        await faulty.put(**args)
    if fault == "cancel_after_commit":
        assert isinstance(caught.value, asyncio.CancelledError)
    else:
        assert getattr(caught.value, "code", None) == "artifact_commit_unknown"
        assert "private storage detail" not in str(caught.value)
    assert blobs.deletes == 0
    assert len(blobs.objects) == 1
    recovered_store = store_for(integration_engine, blobs)
    previous = await recovered_store.get_meta_by_key(args["tenant_id"], args["idempotency_key"])
    assert (previous is None) == (fault == "before_commit")
    winner = await recovered_store.put(**args)
    if previous is not None:
        assert winner == previous
        assert blobs.puts == 1
    meta, content = await recovered_store.get(winner.tenant_id, winner.artifact_id)
    assert content == args["content"]
    assert meta.content_hash == hashlib.sha256(content).hexdigest()
    assert meta.size_bytes == len(content)


async def test_pdf_concurrent_winner_and_loser_cleanup(integration_engine):
    blobs, args = MemoryBlobs(asyncio.Barrier(2)), pdf_args()
    store = store_for(integration_engine, blobs)
    winners = await asyncio.wait_for(asyncio.gather(store.put(**args), store.put(**args)), 10)
    assert winners[0] == winners[1]
    assert blobs.puts == 2 and blobs.deletes == 1 and len(blobs.objects) == 1


@pytest.mark.parametrize("field", ["content", "workflow_run_id"])
async def test_pdf_same_key_different_binding_conflicts(integration_engine, field):
    blobs, args = MemoryBlobs(), pdf_args()
    store = store_for(integration_engine, blobs)
    winner = await store.put(**args)
    with pytest.raises(ArtifactConflictError):
        await store.put(**{**args, field: b"different" if field == "content" else new_id("run")})
    assert await store.get_meta(winner.tenant_id, winner.artifact_id) == winner
    assert blobs.puts == 1 and blobs.deletes == 0


@pytest.mark.parametrize("kind", ["quote_pdf", "email_draft"])
async def test_get_meta_by_key_is_exact_tenant_bound_and_does_not_touch_bytes(integration_engine, kind):
    blobs, args = MemoryBlobs(), pdf_args()
    if kind == "email_draft":
        subject = new_id("enr")
        args.update(kind=GeneratedArtifactKind.EMAIL_DRAFT, subject_ref=subject,
            idempotency_key=f"{subject}:1:draft", generated_by="legacy_agent",
            mime_type="application/vnd.tradeos.email-draft+json")
    store = store_for(integration_engine, blobs)
    winner = await store.put(**args)
    counts = (blobs.gets, blobs.puts, blobs.deletes)
    assert await store.get_meta_by_key(winner.tenant_id, winner.idempotency_key) == winner
    assert await store.get_meta_by_key(new_id("tn"), winner.idempotency_key) is None
    unknown = pdf_args()["idempotency_key"]
    assert await store.get_meta_by_key(winner.tenant_id, unknown) is None
    assert counts == (blobs.gets, blobs.puts, blobs.deletes)


async def test_key_read_failure_is_safe_and_does_not_touch_objects():
    blobs = MemoryBlobs()

    @asynccontextmanager
    async def failing(_tenant):
        raise RuntimeError("private SQL detail")
        yield

    store = GeneratedArtifactStoreImpl(failing, blobs, 10000, lambda: NOW, new_id)
    args = pdf_args()
    with pytest.raises(Exception) as caught:
        await store.get_meta_by_key(args["tenant_id"], args["idempotency_key"])
    assert getattr(caught.value, "code", None) == "artifact_unavailable"
    assert "private SQL detail" not in str(caught.value)
    assert (blobs.gets, blobs.puts, blobs.deletes) == (0, 0, 0)


async def test_unknown_commit_then_database_unavailable_does_not_delete_or_report_success(integration_engine):
    blobs, args = MemoryBlobs(), pdf_args()
    with pytest.raises(Exception) as caught:
        await store_for(integration_engine, blobs, fault="after_commit").put(**args)
    assert caught.value.code == "artifact_commit_unknown"

    @asynccontextmanager
    async def unavailable(_tenant):
        raise RuntimeError("database temporarily unavailable")
        yield

    store = GeneratedArtifactStoreImpl(unavailable, blobs, 10000, lambda: NOW, new_id)
    with pytest.raises(Exception) as caught:
        await store.get_meta_by_key(args["tenant_id"], args["idempotency_key"])
    assert caught.value.code == "artifact_unavailable"
    assert blobs.deletes == 0 and len(blobs.objects) == 1


@pytest.mark.parametrize("key", ["anything", "quo_bad:1:quote_pdf:quote_pdf_v1",
    "enr_00000000000000000000000000:0:draft",
    "quo_00000000000000000000000000:1:quote_pdf:unregistered"])
async def test_key_lookup_rejects_arbitrary_key_without_infrastructure(key):
    from shared.errors import ValidationError

    def unused(_tenant):
        pytest.fail("非法键不得进入基础设施")

    store = GeneratedArtifactStoreImpl(unused, MemoryBlobs(), 10000, lambda: NOW, new_id)
    with pytest.raises(ValidationError):
        await store.get_meta_by_key(new_id("tn"), key)
