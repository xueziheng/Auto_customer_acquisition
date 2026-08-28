"""显式恢复的固定HMAC、窄依赖及新call前置审计。"""

# ruff: noqa: PLC0414 -- 显式导入pytest fixture
import inspect

import pytest

from tests.unit.test_quote_file_gateway import application_case as application_case
from tests.unit.test_quote_file_gateway import snapshot
from tests.unit.test_quote_pdf_artifacts import ULID
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.quote_file_recovery import (
    QuoteFileRecoveryHandler,
    quote_file_recovery_parts,
)
from tool_gateway.handlers.quote_files import (
    quote_file_generation_key,
    quote_file_generation_parts,
)
from tool_gateway.quote_file_ledger import (
    PublicLedgerQuoteRecoveryAudit,
    QuoteFileRecoveryPreflight,
    QuoteGenerationLedgerFact,
)


def original():
    s = snapshot()
    digest, version = HmacFingerprintProvider("test-v1", b"x" * 32).fingerprint(
        quote_file_generation_parts(s)
    )
    return QuoteGenerationLedgerFact(
        tenant_id=s.tenant_id,
        call_id=f"tcl_{ULID}",
        tool_id="quotation.file.generate",
        tool_version="v1",
        idempotency_key=quote_file_generation_key(s),
        request_fingerprint=digest,
        fingerprint_version=version,
        status=ToolCallStatus.EXECUTING,
        provider_ref=None,
    )


def test_recovery_parts_bind_original_generation_protocol_not_new_technical_key():
    s, old = snapshot(), original()
    assert quote_file_recovery_parts(s, old) == tuple(
        part.encode()
        for part in (
            "quote-file-reconcile-v1",
            s.tenant_id,
            "quotation.file.reconcile",
            old.call_id,
            old.idempotency_key,
            old.request_fingerprint,
            old.fingerprint_version,
            s.quote_content_hash,
            s.customer_content_hash,
            s.approval_run_id,
            s.approval_facts_hash,
            s.template_version,
        )
    )


def test_recovery_has_only_metadata_dependency():
    parameters = inspect.signature(QuoteFileRecoveryHandler).parameters
    assert "metadata_only" in parameters
    assert not {"store", "renderer", "transport", "resolver", "client"} & set(
        parameters
    )


@pytest.fixture
def audit_case():
    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace

    from tests.unit.test_quote_pdf_artifacts import NOW
    from tool_gateway.repository import ToolCallRecord

    old = original()
    new_id = f"tcl_{ULID[:-1]}1"
    p = QuoteFileRecoveryPreflight(
        tenant_id=old.tenant_id,
        user_id="emp_manager",
        actor_id="emp_manager",
        quote_id=snapshot().quote_id,
        original_generation_call_id=old.call_id,
        recovery_call_id=new_id,
        recovery_tool_version="v1",
    )
    previous = ToolCallRecord(
        tenant_id=old.tenant_id,
        tool_call_id=old.call_id,
        tool_id=old.tool_id,
        tool_version=old.tool_version,
        risk_level="medium",
        cost_class="free",
        idempotency_key=old.idempotency_key,
        request_fingerprint=old.request_fingerprint,
        fingerprint_version=old.fingerprint_version,
        status=ToolCallStatus.EXECUTING,
        duplicate_of=None,
        lease_owner="worker",
        lease_expires_at=NOW + timedelta(days=1),
        attempt_count=1,
        run_id=None,
        user_id="emp_owner",
        campaign_id=None,
        message_attempt_id=None,
        provider_ref=None,
        error_category=None,
        retry_after_at=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )
    current = replace(
        previous,
        tool_call_id=new_id,
        tool_id="quotation.file.reconcile",
        idempotency_key=f"call:{new_id}",
        request_fingerprint="b" * 64,
        fingerprint_version="test-v1",
        user_id="emp_manager",
    )

    class Calls:
        def __init__(self):
            self.rows = {old.call_id: previous, new_id: current}
            self.events = []

        async def get(self, tenant, call):
            assert tenant == old.tenant_id
            return self.rows.get(call)

        async def append_event(self, event):
            self.events.append(event)

    calls = Calls()

    class Uow:
        async def __aenter__(self):
            self.calls = calls
            return self

        async def __aexit__(self, *args):
            return None

    audit = PublicLedgerQuoteRecoveryAudit(
        lambda tenant: Uow(), now=lambda: NOW, id_factory=lambda prefix: f"tce_{ULID}"
    )
    return SimpleNamespace(
        audit=audit,
        calls=calls,
        preflight=p,
        original=old,
        previous=previous,
        current=current,
    )


async def test_public_audit_appends_only_new_call(audit_case):
    c = audit_case
    await c.audit.append_requested(
        c.original.tenant_id,
        preflight=c.preflight,
        original=c.original,
        request_fingerprint="b" * 64,
        fingerprint_version="test-v1",
    )
    assert len(c.calls.events) == 1
    event = c.calls.events[0]
    assert (
        event.tool_call_id == c.preflight.recovery_call_id
        and event.rule == f"original:{c.original.call_id}"
    )
    assert (
        event.actor_id == c.preflight.user_id
        and event.run_id
        is event.campaign_id
        is event.message_attempt_id
        is event.cost_note
        is None
    )
    assert c.calls.rows[c.original.call_id] == c.previous


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool_id", "quotation.file.read"),
        ("tool_version", "v0"),
        ("status", ToolCallStatus.CLAIMED),
        ("user_id", "emp_other"),
        ("request_fingerprint", "c" * 64),
        ("fingerprint_version", "other-v1"),
        ("idempotency_key", "another-key"),
        ("run_id", "run_other"),
        ("campaign_id", "cmp_other"),
        ("message_attempt_id", "attempt_other"),
    ],
)
async def test_audit_rejects_every_new_call_binding_before_append(
    audit_case, field, value
):
    from dataclasses import replace

    from tool_gateway.quote_file_ledger import QuoteRecoveryAuditError

    c = audit_case
    c.calls.rows[c.current.tool_call_id] = replace(c.current, **{field: value})
    with pytest.raises(QuoteRecoveryAuditError) as error:
        await c.audit.append_requested(
            c.original.tenant_id,
            preflight=c.preflight,
            original=c.original,
            request_fingerprint="b" * 64,
            fingerprint_version="test-v1",
        )
    assert error.value.code == "recovery_audit_binding_invalid" and c.calls.events == []


@pytest.mark.parametrize("value", [0, "false", None])
def test_recovery_wrapper_requires_literal_boolean_false(value):
    from pydantic import ValidationError

    from domains.quotations.schemas import QuoteFileView
    from tests.unit.test_quote_pdf_artifacts import NOW, file_values
    from workflows.quote_approval.file_schemas import QuoteFileRecoveryResult

    with pytest.raises(ValidationError):
        QuoteFileRecoveryResult(
            outcome="metadata_recovered_original_unresolved",
            file=QuoteFileView(**file_values()),
            original_generation_call_id=f"tcl_{ULID}",
            recovery_call_id=f"tcl_{ULID[:-1]}1",
            original_status_at_check="executing",
            original_ledger_modified=value,
            checked_at=NOW,
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "bytes",
        "new_id",
        "old_id",
        "provider",
        "state",
        "clock",
        "failed",
    ],
)
async def test_recovery_handoff_requires_success_and_exact_typed_payload(
    application_case, fault
):
    from tests.unit.test_quote_pdf_artifacts import NOW
    from tool_gateway.errors import ToolErrorCategory
    from tool_gateway.handlers.quote_files import (
        RECOVERY_MANIFEST,
        QuoteFileBytesPayload,
        QuoteFileRecoveryPayload,
    )
    from tool_gateway.pipeline import ToolCallResult

    c = application_case
    old_id, call_id = f"tcl_{ULID}", f"tcl_{ULID[:-1]}1"
    payload = QuoteFileRecoveryPayload(
        file=c.file,
        original_generation_call_id=old_id,
        recovery_call_id=call_id,
        original_status_at_check="executing",
        checked_at=NOW,
    )
    if fault == "missing":
        payload = None
    elif fault == "bytes":
        payload = QuoteFileBytesPayload(file=c.file, content=b"private-marker")
    elif fault in {"new_id", "old_id", "state", "clock"}:
        field, value = {
            "new_id": ("recovery_call_id", old_id),
            "old_id": ("original_generation_call_id", call_id),
            "state": ("original_status_at_check", "succeeded"),
            "clock": ("checked_at", NOW.replace(tzinfo=None)),
        }[fault]
        payload = payload.model_copy(update={field: value})
    c.invoker.payload = payload
    failed = fault == "failed"
    c.invoker.result = ToolCallResult(
        "quotation.file.reconcile",
        ToolCallStatus.FAILED_TRANSIENT if failed else ToolCallStatus.SUCCEEDED,
        tool_call_id=call_id,
        error_category=ToolErrorCategory.RECONCILIATION_REQUIRED if failed else None,
        output=None
        if failed
        else {
            "provider_ref": f"qfl_{ULID[:-1]}1"
            if fault == "provider"
            else c.file.file_id
        },
    )
    assert set(RECOVERY_MANIFEST.output_schema["properties"]) == {"provider_ref"}
    if fault is None:
        result = await c.app.reconcile(
            c.snapshot.tenant_id, c.snapshot.quote_id, old_id, actor_id="emp_owner"
        )
        assert (
            result.outcome == "metadata_recovered_original_unresolved"
            and result.checked_at == NOW
        )
    else:
        with pytest.raises(Exception) as error:
            await c.app.reconcile(
                c.snapshot.tenant_id, c.snapshot.quote_id, old_id, actor_id="emp_owner"
            )
        assert (
            error.value.detail.tool_call_id == call_id
            and "private-marker" not in repr(error.value)
        )
    assert c.slot.take() is None


@pytest.mark.parametrize(
    "fault", ["missing", "mapping", "actor", "quote", "old_id", "same_new_id"]
)
async def test_prepare_rejects_missing_or_forged_preflight_before_authorization(
    audit_case, fault
):
    from tool_gateway.errors import ToolGatewayError
    from tool_gateway.handlers.quote_files import QuoteFileResultSlot
    from tool_gateway.pipeline import ToolCallContext

    c = audit_case

    class NoAccess:
        calls = 0

        async def authorize(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("不得在伪preflight之后判权")

    access, slot = NoAccess(), QuoteFileResultSlot()
    handler = QuoteFileRecoveryHandler(
        access,
        None,
        None,
        None,
        None,
        None,
        slot,
        generate_tool_version="v1",
        now=lambda: None,
    )
    preflight = c.preflight
    if fault == "missing":
        preflight = None
    elif fault == "mapping":
        preflight = c.preflight.model_dump()
    else:
        changes = {
            "actor": {"actor_id": "emp_other"},
            "quote": {"quote_id": f"quo_{ULID[:-1]}1"},
            "old_id": {"original_generation_call_id": c.current.tool_call_id},
            "same_new_id": {"recovery_call_id": c.original.call_id},
        }
        preflight = preflight.model_copy(update=changes[fault])
    ctx = ToolCallContext(
        tenant_id=c.original.tenant_id,
        user_id=c.preflight.user_id,
        tool_id="quotation.file.reconcile",
        params={
            "quote_id": c.preflight.quote_id,
            "original_generation_call_id": c.original.call_id,
        },
    )
    with pytest.raises(ToolGatewayError):
        await handler.prepare(ctx, preflight)
    assert slot.take().code == "invalid_input" and access.calls == 0
