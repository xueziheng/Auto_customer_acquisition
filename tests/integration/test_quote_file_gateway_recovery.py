"""原生成实际artifact提交后暂停；新NONE调用只恢复metadata，旧账本只读。"""

# ruff: noqa: PLC0414 -- 真实fixture链，不重造成功receipt
import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from infra.db.tables import ToolCallEventRow, ToolCallRow
from infra.quote_document_store import GeneratedStoreDocumentMetadataReader
from shared.schemas.identifiers import new_id
from tests.integration.test_quote_file_rate_limit import (
    approval_case as approval_case,
)
from tests.integration.test_quote_file_rate_limit import (
    context_case as context_case,
)
from tests.integration.test_quote_file_rate_limit import (
    file_gateway_case as file_gateway_case,
)
from tests.integration.test_quote_file_rate_limit import (
    freeze_case as freeze_case,
)
from tests.integration.test_quote_file_rate_limit import (
    prepared_quote as prepared_quote,
)
from tests.integration.test_quote_file_rate_limit import (
    quotation_case as quotation_case,
)
from tests.integration.test_quote_file_rate_limit import (
    rate_case as rate_case,
)
from tests.integration.test_quote_file_rate_limit import (
    recovery_case as recovery_case,
)
from tests.integration.test_quote_file_rate_limit import (
    unit_db_case as unit_db_case,
)
from tests.integration.test_quote_file_rate_limit import (
    unit_engine as unit_engine,
)
from tool_gateway.handlers.quote_file_recovery import QuoteFileRecoveryHandler
from tool_gateway.handlers.quote_files import RECOVERY_MANIFEST
from tool_gateway.quote_file_ledger import PublicLedgerQuoteRecoveryAudit


@pytest_asyncio.fixture
async def pending_generation(rate_case, monkeypatch, request):
    c = rate_case
    c.metadata_only = GeneratedStoreDocumentMetadataReader(c.store)
    c.audit = PublicLedgerQuoteRecoveryAudit(
        c.ledger_factory, now=lambda: datetime.now(UTC), id_factory=new_id
    )
    c.recovery = QuoteFileRecoveryHandler(
        c.access,
        c.files,
        c.metadata_only,
        c.ledger,
        c.audit,
        c.fingerprints,
        c.slot,
        generate_tool_version="v1",
        now=lambda: datetime.now(UTC),
    )
    c.registry._items[RECOVERY_MANIFEST.tool_id] = (RECOVERY_MANIFEST, c.recovery)
    reached, c.release = asyncio.Event(), asyncio.Event()
    original_put = c.document_store.put_pdf
    before = getattr(request, "param", "after_put") == "before_put"

    async def paused(*args, **kwargs):
        if before:
            reached.set()
            await c.release.wait()
        meta = await original_put(*args, **kwargs)
        if not before:
            reached.set()
            await c.release.wait()
        return meta

    monkeypatch.setattr(c.document_store, "put_pdf", paused)
    c.old = asyncio.create_task(
        c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    )
    await asyncio.wait_for(reached.wait(), 15)
    async with c.sessions() as db:
        c.original_call_id = await db.scalar(
            select(ToolCallRow.tool_call_id).where(
                ToolCallRow.tenant_id == c.tenant_id, ToolCallRow.status == "executing"
            )
        )
    try:
        yield c
    finally:
        c.release.set()
        await asyncio.gather(c.old, return_exceptions=True)


async def original_state(c):
    async with c.ledger_factory(c.tenant_id) as uow:
        row = await uow.calls.get(c.tenant_id, c.original_call_id)
    async with c.sessions() as db:
        events = list(
            (
                await db.scalars(
                    select(ToolCallEventRow)
                    .where(
                        ToolCallEventRow.tenant_id == c.tenant_id,
                        ToolCallEventRow.tool_call_id == c.original_call_id,
                    )
                    .order_by(ToolCallEventRow.event_id)
                )
            ).all()
        )
    return row, tuple(
        tuple(getattr(event, col.key) for col in ToolCallEventRow.__table__.columns)
        for event in events
    )


async def test_recovery_links_without_changing_old_ledger(
    pending_generation, monkeypatch
):
    c = pending_generation
    before = await original_state(c)
    record = c.files.record_file
    observed = []

    async def audited_record(*args, **kwargs):
        async with c.sessions() as db:
            event = await db.scalar(
                select(ToolCallEventRow).where(
                    ToolCallEventRow.tenant_id == c.tenant_id,
                    ToolCallEventRow.stage == "recovery",
                    ToolCallEventRow.outcome == "requested",
                )
            )
        assert event is not None and event.tool_call_id != c.original_call_id
        assert (
            event.rule == f"original:{c.original_call_id}"
            and event.actor_id == c.manager_id
        )
        observed.append(event.tool_call_id)
        return await record(*args, **kwargs)

    monkeypatch.setattr(c.files, "record_file", audited_record)
    result = await c.app.reconcile(
        c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.manager_id
    )
    assert (
        result.outcome == "metadata_recovered_original_unresolved"
        and result.original_ledger_modified is False
    )
    assert result.original_status_at_check == "executing" and observed == [
        result.recovery_call_id
    ]
    assert (await original_state(c)) == before
    assert c.renderer.calls == c.objects.puts == 1 and c.objects.gets == 0
    assert c.slot.take() is None
    monkeypatch.setattr(c.files, "record_file", record)
    c.release.set()
    assert (await c.old).file_id == result.file.file_id


@pytest.mark.parametrize("pending_generation", ["before_put"], indirect=True)
async def test_no_metadata_does_not_regenerate(pending_generation):
    c = pending_generation
    with pytest.raises(Exception) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    assert (
        error.value.detail.code == "metadata_not_found"
        and error.value.detail.tool_call_id != c.original_call_id
    )
    assert c.objects.puts == c.objects.gets == 0 and c.renderer.calls == 1
    assert await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id) == ()


@pytest.mark.parametrize("failure", ["append", "close", "close_typed", "cancel"])
async def test_audit_failure_or_unknown_is_before_any_file_record(
    pending_generation, monkeypatch, failure
):
    from tool_gateway.quote_file_ledger import QuoteRecoveryAuditError

    c = pending_generation
    before = await original_state(c)
    called = 0
    record = c.files.record_file

    async def counted(*args, **kwargs):
        nonlocal called
        called += 1
        return await record(*args, **kwargs)

    monkeypatch.setattr(c.files, "record_file", counted)

    class Calls:
        def __init__(self, real):
            self.real = real

        async def get(self, *args):
            return await self.real.get(*args)

        async def append_event(self, event):
            if failure == "append":
                raise RuntimeError("controlled-append")
            await self.real.append_event(event)

    class Uow:
        def __init__(self, tenant):
            self.real = c.ledger_factory(tenant)

        async def __aenter__(self):
            real = await self.real.__aenter__()
            self.calls = Calls(real.calls)
            return self

        async def __aexit__(self, *args):
            await self.real.__aexit__(*args)
            if args[0] is None:
                if failure == "cancel":
                    raise asyncio.CancelledError()
                if failure == "close_typed":
                    raise QuoteRecoveryAuditError("recovery_audit_binding_invalid")
                raise RuntimeError("controlled-close")

    c.recovery._audit = PublicLedgerQuoteRecoveryAudit(
        Uow, now=lambda: datetime.now(UTC), id_factory=new_id
    )
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else Exception
    ) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    if failure != "cancel":
        assert error.value.detail.code == (
            "recovery_audit_unavailable"
            if failure == "append"
            else "recovery_audit_unknown"
        )
    assert called == 0 and c.slot.take() is None and (await original_state(c)) == before
    assert await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id) == ()
    async with c.sessions() as db:
        events = list(
            (
                await db.scalars(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == c.tenant_id,
                        ToolCallEventRow.stage == "recovery",
                    )
                )
            ).all()
        )
    assert len(events) == (0 if failure == "append" else 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool_id", "quotation.file.read"),
        ("tool_version", "v0"),
        ("idempotency_key", "wrong-key"),
        ("request_fingerprint", "f" * 64),
        ("fingerprint_version", "old-v1"),
    ],
)
async def test_original_binding_is_real_and_exact(pending_generation, field, value):
    c = pending_generation
    async with c.sessions.begin() as db:
        await db.execute(
            update(ToolCallRow)
            .where(
                ToolCallRow.tenant_id == c.tenant_id,
                ToolCallRow.tool_call_id == c.original_call_id,
            )
            .values(**{field: value})
        )
    before = await original_state(c)
    with pytest.raises(Exception) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    assert error.value.detail.code == "original_binding_invalid"
    assert await original_state(c) == before and c.objects.puts == c.renderer.calls == 1
    assert await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id) == ()


async def test_original_finished_during_recovery_is_not_claimed_as_recovered(
    pending_generation,
):
    c = pending_generation

    class FinishOriginal:
        async def append_requested(self, *args, **kwargs):
            await c.audit.append_requested(*args, **kwargs)
            c.release.set()
            await c.old

    c.recovery._audit = FinishOriginal()
    with pytest.raises(Exception) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    assert error.value.detail.code == "original_state_changed"
    assert (
        len(await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id)) == 1
        and c.slot.take() is None
    )


async def test_concurrent_recovery_converges_on_one_file_and_keeps_old_unresolved(
    pending_generation,
):
    c = pending_generation
    before = await original_state(c)
    results = await asyncio.gather(
        c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        ),
        c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.manager_id
        ),
    )
    assert (
        results[0].file == results[1].file
        and results[0].recovery_call_id != results[1].recovery_call_id
    )
    assert (
        await original_state(c) == before
        and c.renderer.calls == c.objects.puts == 1
        and c.objects.gets == 0
    )


async def test_record_response_unknown_can_be_recovered_again_without_object_io(
    pending_generation, monkeypatch
):
    from domains.quotations.errors import QuoteFileUnavailableError

    c = pending_generation
    record = c.files.record_file

    async def unknown(*args, **kwargs):
        await record(*args, **kwargs)
        raise QuoteFileUnavailableError("storage_unknown")

    monkeypatch.setattr(c.files, "record_file", unknown)
    with pytest.raises(Exception) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    assert error.value.detail.code == "reconciliation_required"
    monkeypatch.setattr(c.files, "record_file", record)
    result = await c.app.reconcile(
        c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
    )
    assert (
        result.file
        == (await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id))[0]
    )
    assert c.objects.puts == c.renderer.calls == 1 and c.objects.gets == 0


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "kind",
        "mime_type",
        "subject_ref",
        "sequence_number",
        "workflow_run_id",
        "generated_by",
        "idempotency_key",
        "artifact_hash",
        "size_bytes",
    ],
)
async def test_corrupt_metadata_never_becomes_recovery_success(
    pending_generation, field
):
    c = pending_generation
    before = await original_state(c)

    class CorruptMetadata:
        async def get_meta_by_key(self, tenant, key):
            from tool_gateway.handlers.quote_files import quote_file_generation_key

            snapshot = await c.access.authorize(tenant, c.quote_id, actor_id=c.sales_id)
            assert key == quote_file_generation_key(snapshot) and not key.startswith(
                "call:"
            )
            meta = await c.metadata_only.get_meta_by_key(tenant, key)
            values = {
                "tenant_id": new_id("tn"),
                "kind": "email_draft",
                "mime_type": "text/plain",
                "subject_ref": new_id("quo"),
                "sequence_number": meta.sequence_number + 1,
                "workflow_run_id": new_id("run"),
                "generated_by": "quote-pdf-v0",
                "idempotency_key": "another-key",
                "artifact_hash": "f" * 64,
                "size_bytes": meta.size_bytes + 1,
            }
            return meta.model_copy(update={field: values[field]})

    c.recovery._metadata = CorruptMetadata()
    with pytest.raises(Exception) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    assert error.value.detail.code == "storage_inconsistent" and c.slot.take() is None
    assert (
        await original_state(c) == before
        and c.renderer.calls == c.objects.puts == 1
        and c.objects.gets == 0
    )
    files = await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    # metadata hash/size只能与T6重新读取的真实收据比较；已合法补关联不删除。
    assert len(files) == (1 if field in {"artifact_hash", "size_bytes"} else 0)


@pytest.mark.parametrize("fault", ["missing", "cross_tenant", "finished"])
async def test_original_missing_other_tenant_or_completed_is_not_recoverable(
    pending_generation, fault
):
    c = pending_generation
    old_id = c.original_call_id
    if fault == "missing":
        old_id = new_id("tcl")
    elif fault == "cross_tenant":
        from tool_gateway.pipeline import ToolCallContext

        other_tenant = new_id("tn")
        result = await c.gateway.invoke(
            ToolCallContext(
                tenant_id=other_tenant,
                user_id=c.sales_id,
                tool_id="quotation.file.generate",
                params={"quote_id": c.quote_id},
                idempotency_key="foreign-test-call",
            )
        )
        old_id = result.tool_call_id
        assert await c.ledger.read(other_tenant, old_id) is not None
        assert await c.ledger.read(c.tenant_id, old_id) is None
    else:
        c.release.set()
        await c.old
    with pytest.raises(Exception) as error:
        await c.app.reconcile(c.tenant_id, c.quote_id, old_id, actor_id=c.sales_id)
    assert error.value.detail.code == (
        "original_state_changed" if fault == "finished" else "original_not_found"
    )
    assert (
        error.value.detail.original_generation_call_id is None and c.slot.take() is None
    )
    assert c.objects.puts == c.renderer.calls == 1 and c.objects.gets == 0


@pytest.mark.parametrize(
    "field,value",
    [("user_id", "emp_other"), ("status", "claimed"), ("tool_version", "v0")],
)
async def test_real_new_call_audit_binding_is_required(
    pending_generation, field, value
):
    c = pending_generation
    before = await original_state(c)

    class ChangedNewCall:
        async def append_requested(self, tenant, **kwargs):
            preflight = kwargs["preflight"]
            async with c.sessions.begin() as db:
                await db.execute(
                    update(ToolCallRow)
                    .where(
                        ToolCallRow.tenant_id == tenant,
                        ToolCallRow.tool_call_id == preflight.recovery_call_id,
                    )
                    .values(**{field: value})
                )
            await c.audit.append_requested(tenant, **kwargs)

    c.recovery._audit = ChangedNewCall()
    with pytest.raises(Exception) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    assert error.value.detail.code == "recovery_audit_binding_invalid"
    assert await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id) == ()
    assert await original_state(c) == before and c.slot.take() is None


@pytest.mark.parametrize("failure", ["complete", "cancel"])
async def test_real_gateway_final_failure_cannot_release_recovery_payload(
    pending_generation, failure
):
    from tool_gateway.errors import ToolCallStatus

    c = pending_generation
    before = await original_state(c)

    class Calls:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        async def complete(self, tenant, call_id, **kwargs):
            row = await self.real.get(tenant, call_id)
            if (
                row.tool_id == "quotation.file.reconcile"
                and kwargs["status"] == ToolCallStatus.SUCCEEDED
            ):
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("private-complete-marker")
            return await self.real.complete(tenant, call_id, **kwargs)

    class Uow:
        def __init__(self, tenant):
            self.real = c.ledger_factory(tenant)

        async def __aenter__(self):
            real = await self.real.__aenter__()
            self.calls = Calls(real.calls)
            return self

        async def __aexit__(self, *args):
            return await self.real.__aexit__(*args)

    c.gateway._uow_factory = Uow
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else Exception
    ) as error:
        await c.app.reconcile(
            c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id
        )
    if failure == "complete":
        assert (
            error.value.detail.code == "storage_inconsistent"
            and error.value.detail.tool_call_id != c.original_call_id
        )
    assert "private-complete-marker" not in repr(error.value) and c.slot.take() is None
    assert await original_state(c) == before
    assert (
        len(await c.files.list_files(c.tenant_id, c.quote_id, actor_id=c.sales_id)) == 1
    )
    assert c.renderer.calls == c.objects.puts == 1 and c.objects.gets == 0


async def test_conflict_exposes_new_received_id_not_original_generation(
    pending_generation,
):
    from tool_gateway.fingerprint import HmacFingerprintProvider

    c = pending_generation
    before = await original_state(c)
    changed = HmacFingerprintProvider("changed-v2", b"y" * 32)
    c.generate._fingerprints = changed
    c.app._fingerprints = changed
    with pytest.raises(Exception) as error:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
    detail = error.value.detail
    assert (
        detail.code == "idempotency_conflict"
        and detail.original_generation_call_id is None
    )
    assert detail.tool_call_id is not None and detail.tool_call_id != c.original_call_id
    async with c.ledger_factory(c.tenant_id) as uow:
        received = await uow.calls.get(c.tenant_id, detail.tool_call_id)
    assert received is not None and received.user_id == c.manager_id
    assert await original_state(c) == before and c.slot.take() is None
    assert c.renderer.calls == c.objects.puts == 1 and c.objects.gets == 0
