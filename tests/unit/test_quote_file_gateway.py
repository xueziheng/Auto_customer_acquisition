"""文件Gateway的互斥槽、固定manifest及跨actor稳定协议。"""

import asyncio
from datetime import timedelta

import pytest

from domains.quotations.schemas import QuoteFileView, QuoteFormalFileSnapshot
from tests.unit.test_quote_pdf_artifacts import NOW, ULID, customer_view, file_values
from tool_gateway.handlers.quote_files import (
    GENERATE_MANIFEST,
    HISTORY_MANIFEST,
    READ_MANIFEST,
    RECOVERY_MANIFEST,
    QuoteFileBytesPayload,
    QuoteFileCallFailure,
    QuoteFileResultSlot,
    QuoteFileResultSlotError,
    quote_file_generation_parts,
    same_quote_file_snapshot,
)


def snapshot():
    return QuoteFormalFileSnapshot(
        tenant_id=f"tn_{ULID}",
        quote_id=f"quo_{ULID}",
        opportunity_id="opp_existing",
        quote_version=1,
        quote_content_hash="a" * 64,
        customer_content_hash="b" * 64,
        approval_run_id=f"run_{ULID}",
        approval_facts_hash="c" * 64,
        template_version="quote_pdf_v1",
        customer=customer_view(),
        checked_at=NOW,
    )


def test_preflight_preserves_real_actor_and_user():
    from tool_gateway.handlers.quote_files import QuoteFilePreflight

    p = QuoteFilePreflight(
        tenant_id=f"tn_{ULID}",
        user_id="emp_owner",
        actor_id="emp_owner",
        tool_id="quotation.file.generate",
        quote_id=f"quo_{ULID}",
        file_id=None,
        snapshot=snapshot(),
        history_file=None,
    )
    assert p.user_id == p.actor_id == "emp_owner"


def test_prepared_rate_and_recovery_preserve_actor_ids():
    from tool_gateway.file_rate_limit import QuoteFileRateRequest
    from tool_gateway.handlers.quote_files import QuoteFilePrepared
    from tool_gateway.quote_file_ledger import QuoteFileRecoveryPreflight

    prepared = QuoteFilePrepared(
        tenant_id=f"tn_{ULID}",
        actor_id="emp_owner",
        tool_id="quotation.file.generate",
        quote_id=f"quo_{ULID}",
        file_id=None,
        snapshot=snapshot(),
        history_file=None,
        rate_claim=None,
    )
    rate = QuoteFileRateRequest(
        canonical_call_id=f"tcl_{ULID}",
        tool_version="v1",
        idempotency_key="original-key",
        request_fingerprint="a" * 64,
        fingerprint_version="test-v1",
        actor_id="emp_manager",
    )
    recovery = QuoteFileRecoveryPreflight(
        tenant_id=f"tn_{ULID}",
        user_id="emp_manager",
        actor_id="emp_manager",
        quote_id=f"quo_{ULID}",
        original_generation_call_id=f"tcl_{ULID}",
        recovery_call_id=f"tcl_{ULID}",
        recovery_tool_version="v1",
    )
    assert (prepared.actor_id, rate.actor_id, recovery.user_id, recovery.actor_id) == (
        "emp_owner",
        "emp_manager",
        "emp_manager",
        "emp_manager",
    )


async def test_slot_is_one_shot_mutually_exclusive_and_task_owned():
    slot = QuoteFileResultSlot()
    payload = QuoteFileBytesPayload(
        file=QuoteFileView(**file_values()), content=b"private-pdf-marker"
    )
    slot.put(payload)
    assert "private-pdf-marker" not in repr(payload)
    with pytest.raises(QuoteFileResultSlotError):
        slot.put(QuoteFileCallFailure(code="invalid_input", retry_after_seconds=None))

    async def child():
        with pytest.raises(QuoteFileResultSlotError):
            slot.take()
        slot.clear()

    await asyncio.create_task(child())
    assert slot.take() == payload and slot.take() is None


def test_generation_protocol_uses_business_binding_not_check_clock():
    a = snapshot()
    b = a.model_copy(update={"checked_at": NOW + timedelta(seconds=1)})
    assert same_quote_file_snapshot(a, b)
    assert quote_file_generation_parts(a) == (
        b"quote-file-v1",
        f"tn_{ULID}".encode(),
        b"quotation.file.generate",
        f"quo_{ULID}".encode(),
        b"1",
        b"quote_pdf_v1",
        b"a" * 64,
        b"b" * 64,
        f"run_{ULID}".encode(),
        b"c" * 64,
    )
    assert quote_file_generation_parts(a) == quote_file_generation_parts(b)


@pytest.mark.parametrize(
    "field",
    [name for name in QuoteFormalFileSnapshot.model_fields if name != "checked_at"],
)
def test_every_business_snapshot_field_is_bound(field):
    a = snapshot()
    old = getattr(a, field)
    changed = (
        old + 1
        if type(old) is int
        else old + "x"
        if type(old) is str
        else old.model_copy(update={"account_name": "Changed"})
    )
    assert not same_quote_file_snapshot(a, a.model_copy(update={field: changed}))


def test_four_tools_have_distinct_fixed_purposes_and_no_client_authority():
    tools = (GENERATE_MANIFEST, READ_MANIFEST, HISTORY_MANIFEST, RECOVERY_MANIFEST)
    assert [m.tool_id for m in tools] == [
        "quotation.file.generate",
        "quotation.file.read",
        "quotation.file.history.read",
        "quotation.file.reconcile",
    ]
    assert [m.requires_approval for m in tools] == [True, True, False, True]
    assert [m.idempotency.value for m in tools] == ["required", "none", "none", "none"]
    for m in tools:
        assert m.version == "v1" and m.high_risk_stage_profile is None
        assert m.input_schema["additionalProperties"] is False
        assert not {
            "actor",
            "role",
            "approved",
            "run",
            "key",
            "template",
            "history",
            "force",
            "customer",
        } & set(m.input_schema["properties"])


@pytest.fixture
def application_case():
    from types import SimpleNamespace

    from tool_gateway.fingerprint import HmacFingerprintProvider
    from workflows.quote_approval.files import QuoteFilesApplication

    s = snapshot()
    file = QuoteFileView(**file_values()).model_copy(
        update={
            "quote_content_hash": s.quote_content_hash,
            "customer_content_hash": s.customer_content_hash,
        }
    )
    slot = QuoteFileResultSlot()

    class Access:
        async def authorize(self, *args, **kwargs):
            return s

        async def authorize_history(self, *args, **kwargs):
            return file

    class Files:
        calls = 0

        async def get_file(self, *args, **kwargs):
            self.calls += 1
            return file

    class Invoker:
        result = None
        payload = None
        error = None

        async def invoke(self, ctx):
            if self.payload is not None:
                slot.put(self.payload)
            if self.error is not None:
                raise self.error
            return self.result

    invoker, access, files = Invoker(), Access(), Files()
    app = QuoteFilesApplication(
        invoker,
        access,
        files,
        slot,
        None,
        HmacFingerprintProvider("test-v1", b"x" * 32),
        generate_tool_version="v1",
    )
    return SimpleNamespace(
        app=app,
        slot=slot,
        invoker=invoker,
        file=file,
        snapshot=s,
        files=files,
        access=access,
    )


@pytest.mark.parametrize("payload_kind", ["empty", "bytes", "recovery", "failure"])
async def test_generate_success_requires_empty_slot(application_case, payload_kind):
    from tool_gateway.errors import ToolCallStatus
    from tool_gateway.handlers.quote_files import QuoteFileRecoveryPayload
    from tool_gateway.pipeline import ToolCallResult
    from workflows.quote_approval.file_schemas import QuoteFileApplicationError

    c = application_case
    c.invoker.result = ToolCallResult(
        "quotation.file.generate",
        ToolCallStatus.SUCCEEDED,
        output={"provider_ref": c.file.file_id},
        tool_call_id=f"tcl_{ULID}",
    )
    c.invoker.payload = {
        "empty": None,
        "bytes": QuoteFileBytesPayload(file=c.file, content=b"PDF"),
        "recovery": QuoteFileRecoveryPayload(
            file=c.file,
            original_generation_call_id=f"tcl_{ULID}",
            recovery_call_id=f"tcl_{ULID}",
            original_status_at_check="executing",
            checked_at=NOW,
        ),
        "failure": QuoteFileCallFailure(code="not_found", retry_after_seconds=None),
    }[payload_kind]
    if payload_kind == "empty":
        assert (
            await c.app.generate(
                c.snapshot.tenant_id, c.snapshot.quote_id, actor_id="emp_owner"
            )
            == c.file
        )
        assert c.files.calls == 1
    else:
        with pytest.raises(QuoteFileApplicationError) as error:
            await c.app.generate(
                c.snapshot.tenant_id, c.snapshot.quote_id, actor_id="emp_owner"
            )
        assert error.value.detail.code == "storage_inconsistent" and c.files.calls == 0
    assert c.slot.take() is None


@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), RuntimeError("private-marker")]
)
async def test_invocation_failure_or_cancel_never_returns_pending_bytes(
    application_case, error
):
    c = application_case
    c.invoker.payload = QuoteFileBytesPayload(file=c.file, content=b"private-marker")
    c.invoker.error = error
    with pytest.raises(
        type(error) if isinstance(error, asyncio.CancelledError) else Exception
    ) as caught:
        await c.app.download(
            c.snapshot.tenant_id,
            c.snapshot.quote_id,
            c.file.file_id,
            actor_id="emp_owner",
        )
    assert "private-marker" not in repr(caught.value) and c.slot.take() is None


@pytest.mark.parametrize("payload_kind", ["empty", "bytes", "failure"])
async def test_non_success_never_releases_bytes(application_case, payload_kind):
    from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
    from tool_gateway.pipeline import ToolCallResult

    c = application_case
    c.invoker.result = ToolCallResult(
        "quotation.file.read",
        ToolCallStatus.FAILED_TRANSIENT,
        tool_call_id=f"tcl_{ULID}",
        error_category=ToolErrorCategory.RECONCILIATION_REQUIRED,
    )
    c.invoker.payload = {
        "empty": None,
        "bytes": QuoteFileBytesPayload(file=c.file, content=b"private-marker"),
        "failure": QuoteFileCallFailure(code="lock_timeout", retry_after_seconds=None),
    }[payload_kind]
    with pytest.raises(Exception) as error:
        await c.app.download(
            c.snapshot.tenant_id,
            c.snapshot.quote_id,
            c.file.file_id,
            actor_id="emp_owner",
        )
    assert error.value.detail.tool_call_id == f"tcl_{ULID}" and c.slot.take() is None
    assert (
        error.value.detail.code
        == {
            "empty": "reconciliation_required",
            "bytes": "storage_inconsistent",
            "failure": "lock_timeout",
        }[payload_kind]
    )


async def test_generate_postcheck_keeps_business_failure_and_real_call_id(
    application_case,
):
    from domains.quotations.errors import QuoteFileAccessError
    from tool_gateway.errors import ToolCallStatus
    from tool_gateway.pipeline import ToolCallResult

    c = application_case

    class Expiring:
        calls = 0

        async def authorize(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise QuoteFileAccessError("quote_expired")
            return c.snapshot

    c.app._access = Expiring()
    c.invoker.result = ToolCallResult(
        "quotation.file.generate",
        ToolCallStatus.SUCCEEDED,
        output={"provider_ref": c.file.file_id},
        tool_call_id=f"tcl_{ULID}",
    )
    with pytest.raises(Exception) as error:
        await c.app.generate(
            c.snapshot.tenant_id, c.snapshot.quote_id, actor_id="emp_owner"
        )
    assert (
        error.value.detail.code == "quote_expired"
        and error.value.detail.tool_call_id == f"tcl_{ULID}"
    )
    assert c.slot.take() is None


async def test_matching_claimed_short_circuit_is_not_a_fingerprint_conflict(
    application_case,
):
    from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
    from tool_gateway.handlers.quote_files import quote_file_generation_key
    from tool_gateway.pipeline import ToolCallResult
    from tool_gateway.quote_file_ledger import QuoteGenerationLedgerFact

    c = application_case
    digest, version = c.app._fingerprints.fingerprint(
        quote_file_generation_parts(c.snapshot)
    )

    class Ledger:
        async def read(self, tenant, call):
            return QuoteGenerationLedgerFact(
                tenant_id=tenant,
                call_id=call,
                tool_id="quotation.file.generate",
                tool_version="v1",
                idempotency_key=quote_file_generation_key(c.snapshot),
                request_fingerprint=digest,
                fingerprint_version=version,
                status=ToolCallStatus.CLAIMED,
                provider_ref=None,
            )

    c.app._ledger = Ledger()
    c.invoker.result = ToolCallResult(
        "quotation.file.generate",
        ToolCallStatus.FAILED_TRANSIENT,
        tool_call_id=f"tcl_{ULID}",
        error_category=ToolErrorCategory.IN_PROGRESS,
    )
    with pytest.raises(Exception) as error:
        await c.app.generate(
            c.snapshot.tenant_id, c.snapshot.quote_id, actor_id="emp_owner"
        )
    assert error.value.detail.code == "reconciliation_required"
    assert error.value.detail.original_generation_call_id is None
