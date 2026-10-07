"""真实隔离PG metadata与受控bytes；不运行PDF作者或外部Provider。"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.errors import ArtifactConflictError
from artifact_store.service_impl import GeneratedArtifactStoreImpl
from artifact_store.store import GeneratedArtifactKind
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from shared.schemas.identifiers import new_id
from tests.integration.test_quote_approval_postgres import (
    approval_case as approval_case,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)
from tests.integration.test_quote_approval_postgres import (
    context_case as context_case,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)
from tests.integration.test_quote_approval_postgres import (
    freeze_case as freeze_case,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)
from tests.integration.test_quote_approval_postgres import (
    prepared_quote as prepared_quote,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)
from tests.integration.test_quote_approval_postgres import (
    quotation_case as quotation_case,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)
from tests.integration.test_quote_approval_postgres import (
    recovery_case as recovery_case,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)
from tests.integration.test_quote_approval_postgres import (
    unit_db_case as unit_db_case,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)
from tests.integration.test_quote_approval_postgres import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- pytest显式重导出真实依赖链
)

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
    return {
        "tenant_id": new_id("tn"),
        "kind": GeneratedArtifactKind.QUOTE_PDF,
        "content": b"controlled-pdf-bytes",
        "mime_type": "application/pdf",
        "workflow_run_id": new_id("run"),
        "subject_ref": quote,
        "sequence_number": 1,
        "idempotency_key": f"{quote}:1:quote_pdf:quote_pdf_v1",
        "generated_by": "quote_pdf_v1",
    }


def store_for(engine, blobs: MemoryBlobs, *, fault: str | None = None):
    class CloseFailedSession(AsyncSession):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("private close detail")

    sessions = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=CloseFailedSession if fault == "close_failed" else AsyncSession,
    )

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


@pytest.mark.parametrize(
    "fault", ["after_commit", "cancel_after_commit", "before_commit", "close_failed"]
)
async def test_pdf_unknown_commit_preserves_bytes_and_original_key_recovers(
    unit_engine, fault
):
    blobs, args = MemoryBlobs(), pdf_args()
    faulty = store_for(unit_engine, blobs, fault=fault)
    with pytest.raises(BaseException) as caught:
        await faulty.put(**args)
    if fault == "cancel_after_commit":
        assert isinstance(caught.value, asyncio.CancelledError)
    else:
        assert getattr(caught.value, "code", None) == "artifact_commit_unknown"
        assert "private storage detail" not in str(caught.value)
    assert blobs.deletes == 0
    assert len(blobs.objects) == 1
    recovered_store = store_for(unit_engine, blobs)
    previous = await recovered_store.get_meta_by_key(
        args["tenant_id"], args["idempotency_key"]
    )
    assert (previous is None) == (fault == "before_commit")
    winner = await recovered_store.put(**args)
    if previous is not None:
        assert winner == previous
        assert blobs.puts == 1
    meta, content = await recovered_store.get(winner.tenant_id, winner.artifact_id)
    assert content == args["content"]
    assert meta.content_hash == hashlib.sha256(content).hexdigest()
    assert meta.size_bytes == len(content)


async def test_pdf_concurrent_winner_and_loser_cleanup(unit_engine):
    blobs, args = MemoryBlobs(asyncio.Barrier(2)), pdf_args()
    store = store_for(unit_engine, blobs)
    winners = await asyncio.wait_for(
        asyncio.gather(store.put(**args), store.put(**args)), 10
    )
    assert winners[0] == winners[1]
    assert blobs.puts == 2 and blobs.deletes == 1 and len(blobs.objects) == 1


@pytest.mark.parametrize("field", ["content", "workflow_run_id"])
async def test_pdf_same_key_different_binding_conflicts(unit_engine, field):
    blobs, args = MemoryBlobs(), pdf_args()
    store = store_for(unit_engine, blobs)
    winner = await store.put(**args)
    with pytest.raises(ArtifactConflictError):
        await store.put(
            **{**args, field: b"different" if field == "content" else new_id("run")}
        )
    assert await store.get_meta(winner.tenant_id, winner.artifact_id) == winner
    assert blobs.puts == 1 and blobs.deletes == 0


@pytest.mark.parametrize("kind", ["quote_pdf", "email_draft"])
async def test_get_meta_by_key_is_exact_tenant_bound_and_does_not_touch_bytes(
    unit_engine, kind
):
    blobs, args = MemoryBlobs(), pdf_args()
    if kind == "email_draft":
        subject = new_id("enr")
        args.update(
            kind=GeneratedArtifactKind.EMAIL_DRAFT,
            subject_ref=subject,
            idempotency_key=f"{subject}:1:draft",
            generated_by="legacy_agent",
            mime_type="application/vnd.tradeos.email-draft+json",
        )
    store = store_for(unit_engine, blobs)
    winner = await store.put(**args)
    counts = (blobs.gets, blobs.puts, blobs.deletes)
    assert (
        await store.get_meta_by_key(winner.tenant_id, winner.idempotency_key) == winner
    )
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


async def test_unknown_commit_then_database_unavailable_does_not_delete_or_report_success(
    unit_engine,
):
    blobs, args = MemoryBlobs(), pdf_args()
    with pytest.raises(Exception) as caught:
        await store_for(unit_engine, blobs, fault="after_commit").put(**args)
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


@pytest.mark.parametrize(
    "key",
    [
        "anything",
        "quo_bad:1:quote_pdf:quote_pdf_v1",
        "enr_00000000000000000000000000:0:draft",
        "quo_00000000000000000000000000:1:quote_pdf:unregistered",
    ],
)
async def test_key_lookup_rejects_arbitrary_key_without_infrastructure(key):
    from shared.errors import ValidationError

    def unused(_tenant):
        pytest.fail("非法键不得进入基础设施")

    store = GeneratedArtifactStoreImpl(
        unused, MemoryBlobs(), 10000, lambda: NOW, new_id
    )
    with pytest.raises(ValidationError):
        await store.get_meta_by_key(new_id("tn"), key)


@pytest_asyncio.fixture
async def approved_file_case(approval_case, unit_engine):
    from types import SimpleNamespace

    from tests.integration.test_quote_approval_postgres import bind_real_approvals
    from tests.unit.test_quote_file_service import ControlledScope
    from workflows.quote_approval.approvals import read_quote_facts
    from workflows.quote_approval.run_reader import WorkflowQuoteRunReader

    c = approval_case
    ids = await bind_real_approvals(c)
    for approval_id in ids:
        await c.approvals.decide(
            c.quotation.tenant, approval_id, approved=True, decided_by=c.decider
        )
    facts = await read_quote_facts(c.approvals, c.quotation.tenant, ids)
    async with (
        c.provider.open_for_approval(
            c.quotation.tenant,
            c.quote.content.opportunity_id,
            c.decider,
            prepared_by=c.quote.content.prepared_by,
            decider_ids=(c.decider,),
        ) as context,
        c.quotation.service.open_approval(
            c.quotation.tenant, c.quote.content.quote_id, executor=c.executor
        ) as session,
    ):
        result = await session.apply(facts, context)
    blobs = MemoryBlobs()
    store = store_for(unit_engine, blobs)
    quote = result.quote
    args = pdf_args()
    args.update(
        tenant_id=c.quotation.tenant,
        subject_ref=quote.content.quote_id,
        sequence_number=quote.content.version,
        workflow_run_id=result.receipt.approval_run_id,
        idempotency_key=f"{quote.content.quote_id}:{quote.content.version}:quote_pdf:quote_pdf_v1",
    )
    meta = await store.put(**args)
    return SimpleNamespace(
        approval=c,
        quote=quote,
        receipt=result.receipt,
        meta=meta,
        store=store,
        blobs=blobs,
        scope=ControlledScope([]),
        runs=WorkflowQuoteRunReader(lambda: c.engine),
        tenant=c.quotation.tenant,
        actor_id=c.quotation.actor.employee_id,
        engine=unit_engine,
    )


def file_service_for(c, *, factory=None):
    import importlib

    assert importlib.util.find_spec("domains.quotations.file_service"), (
        "缺少文件关联服务"
    )
    from domains.quotations.file_service import QuoteFileServiceImpl
    from infra.quote_file_artifacts import GeneratedStoreQuoteArtifactReader

    return QuoteFileServiceImpl(
        factory or c.approval.quotation.factory,
        c.approval.quotation.actors,
        c.scope,
        GeneratedStoreQuoteArtifactReader(c.store),
        c.runs,
        id_generator=new_id,
    )


async def test_record_real_approved_pdf_restarts_and_reads_completed_run(
    approved_file_case,
):
    c = approved_file_case
    service = file_service_for(c)
    first = await service.record_file(
        c.tenant, c.quote.content.quote_id, c.meta.artifact_id, actor_id=c.actor_id
    )
    assert await c.approval.engine.poll_due(c.tenant, limit=1) == 1
    run = await c.approval.engine.get_run(c.tenant, c.receipt.approval_run_id)
    assert run.status.value == "completed"
    restarted = file_service_for(c)
    assert (
        await restarted.record_file(
            c.tenant, c.quote.content.quote_id, c.meta.artifact_id, actor_id=c.actor_id
        )
        == first
    )
    fact = await restarted.get_file_approval(
        c.tenant, c.quote.content.quote_id, actor_id=c.actor_id
    )
    assert fact.approval_run_id == c.receipt.approval_run_id
    assert fact.approval_facts_hash == c.receipt.facts_hash
    assert (
        await restarted.get_file(
            c.tenant, c.quote.content.quote_id, first.file_id, actor_id=c.actor_id
        )
        == first
    )
    assert await restarted.list_files(
        c.tenant, c.quote.content.quote_id, actor_id=c.actor_id
    ) == (first,)
    assert c.blobs.gets == 0 and c.blobs.puts == 1
    assert first.content_hash == c.meta.content_hash
    assert first.quote_content_hash == c.quote.content.content_hash


async def test_real_file_commit_lost_reply_recovers_original_artifact(
    approved_file_case,
):
    c = approved_file_case
    original = c.approval.quotation.factory

    @asynccontextmanager
    async def lost_reply(tenant):
        async with original(tenant) as uow:
            yield uow
            committed = uow._committed
        if committed:
            raise RuntimeError("private close detail")

    with pytest.raises(Exception) as error:
        await file_service_for(c, factory=lost_reply).record_file(
            c.tenant, c.quote.content.quote_id, c.meta.artifact_id, actor_id=c.actor_id
        )
    assert getattr(error.value, "code", None) == "storage_unknown"
    winner = await file_service_for(c).record_file(
        c.tenant, c.quote.content.quote_id, c.meta.artifact_id, actor_id=c.actor_id
    )
    assert await file_service_for(c).list_files(
        c.tenant, c.quote.content.quote_id, actor_id=c.actor_id
    ) == (winner,)
    assert c.blobs.puts == 1 and c.blobs.deletes == 0


def sql_file_values(c):
    return {
        "tenant_id": c.tenant,
        "file_id": new_id("qfl"),
        "quote_id": c.quote.content.quote_id,
        "quote_version": c.quote.content.version,
        "artifact_id": c.meta.artifact_id,
        "quote_content_hash": c.quote.content.content_hash,
        "customer_content_hash": "c" * 64,
        "artifact_hash": c.meta.content_hash,
        "template_version": c.meta.generated_by,
        "approval_run_id": c.receipt.approval_run_id,
        "size_bytes": c.meta.size_bytes,
        "generated_at": c.meta.generated_at,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quote_version", 2),
        ("quote_content_hash", "f" * 64),
        ("artifact_hash", "f" * 64),
        ("size_bytes", 999),
        ("approval_run_id", "run_00000000000000000000000000"),
    ],
)
async def test_sql_existing_parents_do_not_allow_wrong_file_binding(
    approved_file_case, field, value
):
    from sqlalchemy import insert
    from sqlalchemy.exc import DBAPIError

    from infra.db.tables import QuotationFileRow

    c = approved_file_case
    async with c.engine.begin() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(
                insert(QuotationFileRow).values(**{**sql_file_values(c), field: value})
            )


async def test_wrong_customer_hash_is_rejected_on_read_and_files_are_immutable(
    approved_file_case,
):
    from sqlalchemy import delete, insert, update
    from sqlalchemy.exc import DBAPIError

    from infra.db.tables import QuotationFileRow

    c = approved_file_case
    values = sql_file_values(c)
    async with c.engine.begin() as conn:
        await conn.execute(insert(QuotationFileRow).values(**values))
    for statement in (
        update(QuotationFileRow).values(size_bytes=999),
        delete(QuotationFileRow),
    ):
        async with c.engine.begin() as conn:
            with pytest.raises(DBAPIError):
                await conn.execute(
                    statement.where(
                        QuotationFileRow.tenant_id == c.tenant,
                        QuotationFileRow.file_id == values["file_id"],
                    )
                )
    service = file_service_for(c)
    for method, args in [
        (service.get_file, (values["file_id"],)),
        (service.list_files, ()),
    ]:
        with pytest.raises(Exception) as error:
            await method(c.tenant, c.quote.content.quote_id, *args, actor_id=c.actor_id)
        assert getattr(error.value, "code", None) == "storage_inconsistent"


async def test_real_concurrent_file_record_has_one_winner_and_scoped_commit(
    approved_file_case,
):
    from domains.quotations.file_service import QuoteFileServiceImpl
    from infra.quote_file_artifacts import GeneratedStoreQuoteArtifactReader

    c = approved_file_case
    barrier = asyncio.Barrier(2)
    original = c.approval.quotation.factory

    class GuardedUow:
        def __init__(self, delegate):
            self.delegate, self.quotes = delegate, delegate.quotes

        async def commit(self):
            assert c.scope.depth == 1
            await self.delegate.commit()

    @asynccontextmanager
    async def uows(tenant):
        async with original(tenant) as uow:
            yield GuardedUow(uow)

    class Reader:
        async def read(self, tenant, artifact):
            result = await GeneratedStoreQuoteArtifactReader(c.store).read(
                tenant, artifact
            )
            assert c.scope.depth == 1
            await barrier.wait()
            return result

    service = QuoteFileServiceImpl(
        uows,
        c.approval.quotation.actors,
        c.scope,
        Reader(),
        c.runs,
        id_generator=new_id,
    )
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                service.record_file(
                    c.tenant,
                    c.quote.content.quote_id,
                    c.meta.artifact_id,
                    actor_id=c.actor_id,
                )
                for _ in range(2)
            )
        ),
        10,
    )
    assert results[0] == results[1]
    assert await file_service_for(c).list_files(
        c.tenant, c.quote.content.quote_id, actor_id=c.actor_id
    ) == (results[0],)
    assert c.blobs.gets == 0 and c.blobs.puts == 1


async def test_real_metadata_reader_runs_outside_quote_advisory_lock(
    approved_file_case,
):
    import json

    from sqlalchemy import text

    from domains.quotations.file_service import QuoteFileServiceImpl
    from infra.quote_file_artifacts import GeneratedStoreQuoteArtifactReader

    c = approved_file_case

    class Reader:
        async def read(self, tenant, artifact):
            async with c.engine.begin() as conn:
                assert await conn.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
                    {
                        "key": json.dumps(
                            [
                                "quotation-create-v1",
                                tenant,
                                c.quote.content.opportunity_id,
                            ],
                            separators=(",", ":"),
                        )
                    },
                )
            return await GeneratedStoreQuoteArtifactReader(c.store).read(
                tenant, artifact
            )

    service = QuoteFileServiceImpl(
        c.approval.quotation.factory,
        c.approval.quotation.actors,
        c.scope,
        Reader(),
        c.runs,
        id_generator=new_id,
    )
    await service.record_file(
        c.tenant, c.quote.content.quote_id, c.meta.artifact_id, actor_id=c.actor_id
    )


async def test_file_history_survives_need_change_and_quote_expiry_without_new_state_events(
    approved_file_case,
):
    from datetime import timedelta

    from sqlalchemy import func, select, text

    from infra.db.tables import QuotationStateEventRow

    c = approved_file_case
    service = file_service_for(c)
    first = await service.record_file(
        c.tenant, c.quote.content.quote_id, c.meta.artifact_id, actor_id=c.actor_id
    )
    c.approval.quotation.clock[0] += timedelta(days=30)
    assert await c.approval.quotation.service.expire_overdue(c.tenant, limit=10) == 1
    await c.approval.creation.freeze.change_material()
    async with c.engine.begin() as conn:
        before = await conn.scalar(
            select(func.count())
            .select_from(QuotationStateEventRow)
            .where(QuotationStateEventRow.tenant_id == c.tenant)
        )
    assert (
        await service.get_file(
            c.tenant, c.quote.content.quote_id, first.file_id, actor_id=c.actor_id
        )
        == first
    )
    assert (
        await service.get_file_approval(
            c.tenant, c.quote.content.quote_id, actor_id=c.actor_id
        )
    ).approval_run_id == c.receipt.approval_run_id
    assert (
        await service.record_file(
            c.tenant, c.quote.content.quote_id, c.meta.artifact_id, actor_id=c.actor_id
        )
        == first
    )
    async with c.engine.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(QuotationStateEventRow)
                .where(QuotationStateEventRow.tenant_id == c.tenant)
            )
            == before
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT state FROM quotations WHERE tenant_id=:tenant AND quote_id=:quote"
                ),
                {"tenant": c.tenant, "quote": c.quote.content.quote_id},
            )
            == "expired"
        )
    assert c.blobs.puts == 1 and c.blobs.gets == 0


async def test_adapter_keeps_real_email_kind_and_hides_cross_tenant_metadata(
    unit_engine,
):
    from infra.quote_file_artifacts import GeneratedStoreQuoteArtifactReader

    blobs, args = MemoryBlobs(), pdf_args()
    subject = new_id("enr")
    args.update(
        kind=GeneratedArtifactKind.EMAIL_DRAFT,
        subject_ref=subject,
        idempotency_key=f"{subject}:1:draft",
        generated_by="outreach_v1",
        mime_type="application/vnd.tradeos.email-draft+json",
    )
    store = store_for(unit_engine, blobs)
    meta = await store.put(**args)
    reader = GeneratedStoreQuoteArtifactReader(store)
    fact = await reader.read(meta.tenant_id, meta.artifact_id)
    assert fact.kind == "email_draft"
    assert fact.mime_type == "application/vnd.tradeos.email-draft+json"
    assert await reader.read(new_id("tn"), meta.artifact_id) is None
    assert blobs.gets == 0 and blobs.puts == 1 and blobs.deletes == 0


@pytest.mark.parametrize("cancelled", [False, True])
async def test_transport_unknown_after_put_retains_candidate_and_does_not_retry(
    unit_engine, cancelled
):
    class FailedTransport(MemoryBlobs):
        async def put(self, object_key, content):
            await super().put(object_key, content)
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("private transport detail")

    blobs, args = FailedTransport(), pdf_args()
    with pytest.raises(BaseException) as error:
        await store_for(unit_engine, blobs).put(**args)
    if cancelled:
        assert isinstance(error.value, asyncio.CancelledError)
    else:
        assert error.value.code == "artifact_commit_unknown"
    assert blobs.puts == 1 and blobs.deletes == 0 and len(blobs.objects) == 1
    assert (
        await store_for(unit_engine, blobs).get_meta_by_key(
            args["tenant_id"], args["idempotency_key"]
        )
        is None
    )


async def test_unavailable_before_object_put_is_not_commit_unknown():
    blobs, args = MemoryBlobs(), pdf_args()

    @asynccontextmanager
    async def unavailable(_tenant):
        raise RuntimeError("private connection detail")
        yield

    store = GeneratedArtifactStoreImpl(unavailable, blobs, 10000, lambda: NOW, new_id)
    with pytest.raises(Exception) as error:
        await store.put(**args)
    assert error.value.code == "artifact_unavailable"
    assert blobs.puts == 0 and blobs.deletes == 0


@pytest.mark.parametrize("cross_tenant", [False, True])
async def test_existing_unrelated_artifact_cannot_bind_to_approved_quote(approved_file_case, cross_tenant):
    from sqlalchemy import insert
    from sqlalchemy.exc import DBAPIError

    from infra.db.tables import QuotationFileRow

    c, args = approved_file_case, pdf_args()
    if not cross_tenant:
        args["tenant_id"] = c.tenant
    other = await c.store.put(**args)
    values = {**sql_file_values(c), "artifact_id": other.artifact_id}
    async with c.engine.begin() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(insert(QuotationFileRow).values(**values))
    with pytest.raises(Exception) as error:
        await file_service_for(c).record_file(c.tenant, c.quote.content.quote_id,
            other.artifact_id, actor_id=c.actor_id)
    assert error.value.code == ("not_found" if cross_tenant else "metadata_mismatch")
