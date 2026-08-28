"""文件用途独立授权、原批准绑定与三hash的受控端口测试。"""

import importlib
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domains.quotations import schemas as q
from domains.quotations import service as public
from shared.errors import PermissionDenied
from shared.schemas.identifiers import new_id
from tests.unit.test_quotation_service import service_case
from tests.unit.test_quote_approval_contracts import DECIDER, QUOTE, RUN
from tests.unit.test_quote_approval_workflow import bind_case, core_case


class ControlledScope:
    def __init__(self, order: list[str]) -> None:
        self.order, self.denied = order, False
        self._depth = ContextVar("quote_file_scope", default=0)
        self.expected = None

    @property
    def depth(self) -> int:
        return self._depth.get()

    @asynccontextmanager
    async def guard(self, tenant, opportunity, *, actor_id):
        if self.expected is not None:
            assert (tenant, opportunity, actor_id) == self.expected
        if self.denied:
            raise PermissionDenied("private actor detail")
        token = self._depth.set(self.depth + 1)
        self.order.append("scope")
        try:
            yield
        finally:
            self._depth.reset(token)
            self.order.append("release")


async def file_case():
    assert importlib.util.find_spec("domains.quotations.file_service"), (
        "缺少独立文件用途服务"
    )
    core, executor, context, store, uow, runs, order = core_case()
    facts = await bind_case(core, executor, context)
    tenant, actor_id = new_id("tn"), new_id("emp")
    content = store["quote"].content.model_copy(
        update={"tenant_id": tenant, "prepared_by": actor_id}
    )
    content = content.model_copy(
        update={"content_hash": public.quote_content_hash(content)}
    )
    store["quote"] = store["quote"].model_copy(update={"content": content})
    payloads = {
        p.approval_type: p for p in public.quote_approval_payloads(store["quote"], None)
    }
    facts = tuple(
        f.model_copy(
            update={
                "tenant_id": tenant,
                "prepared_by": actor_id,
                "payload": payloads[f.approval_type],
                "change_set_ref": public.quote_change_set_ref(
                    QUOTE, content.content_hash, f.approval_type
                ),
            }
        )
        for f in facts
    )
    store["bindings"] = facts
    uow.quotes.list_versions.return_value = (store["quote"],)
    runs.read.return_value = runs.read.return_value.model_copy(
        update={"tenant_id": tenant, "content_hash": content.content_hash}
    )
    approved = tuple(
        f.model_copy(
            update={
                "decision": "approve",
                "decided_by": DECIDER,
                "decided_at": context.issuer.confirmed_at,
                "state": "approved",
            }
        )
        for f in facts
    )
    store["receipt"] = q.QuoteApprovalApplicationReceipt(
        tenant_id=tenant,
        quote_id=QUOTE,
        quote_version=1,
        content_hash=store["quote"].content.content_hash,
        facts_hash=public.quote_approval_facts_hash(approved),
        decisions=tuple(
            q.QuoteApprovalDecisionSnapshot(
                **{
                    k: getattr(f, k)
                    for k in q.QuoteApprovalDecisionSnapshot.model_fields
                }
            )
            for f in approved
        ),
        applied_at=context.issuer.confirmed_at,
        quote_send_decider=DECIDER,
        approval_run_id=RUN,
    )
    scope = ControlledScope(order)
    scope.expected = (tenant, content.opportunity_id, actor_id)
    actor = context.runtime.current_actor.model_copy(
        update={"role": "sales", "tenant_id": tenant, "employee_id": actor_id}
    )
    actors = AsyncMock()
    actors.read_current.return_value = actor
    reader = AsyncMock()
    meta = q.QuoteGeneratedArtifactFact(
        tenant_id=tenant,
        artifact_id=new_id("art"),
        kind="quote_pdf",
        artifact_hash="a" * 64,
        size_bytes=4,
        mime_type="application/pdf",
        workflow_run_id=RUN,
        subject_ref=QUOTE,
        sequence_number=1,
        idempotency_key=f"{QUOTE}:1:quote_pdf:quote_pdf_v1",
        generated_by="quote_pdf_v1",
        generated_at=context.issuer.confirmed_at,
    )

    async def read(*args):
        assert scope.depth == 1
        assert args == (tenant, meta.artifact_id)
        order.append("metadata")
        return reader.fact

    reader.fact, reader.read.side_effect = meta, read
    files = []
    uow.quotes.file_by_template.side_effect = lambda *args: files[0] if files else None
    uow.quotes.file_by_id.side_effect = lambda *args: files[0] if files else None
    uow.quotes.files_for_quote.side_effect = lambda *args: tuple(files)
    uow.quotes.add_file.side_effect = lambda tenant, record: files.append(record)
    uow.quotes.lock_opportunity.side_effect = lambda *args: order.append("quote_lock")
    impl = importlib.import_module(
        "domains.quotations.file_service"
    ).QuoteFileServiceImpl

    @asynccontextmanager
    async def factory(request_tenant):
        assert request_tenant == tenant
        yield uow

    service = impl(factory, actors, scope, reader, runs, id_generator=new_id)
    order.clear()
    return SimpleNamespace(
        service=service,
        actor=actor,
        actors=actors,
        scope=scope,
        reader=reader,
        meta=meta,
        store=store,
        uow=uow,
        runs=runs,
        order=order,
        files=files,
        tenant=tenant,
        actor_id=actor.employee_id,
    )


@pytest.mark.parametrize(
    "method", ["record_file", "get_file", "list_files", "get_file_approval"]
)
async def test_uncomposed_facade_fails_closed(method):
    service, actors, repo = service_case()
    fn = getattr(service, method, None)
    assert callable(fn), "缺少文件门面"
    args = (
        ("tenant", "quote", "file")
        if method in {"record_file", "get_file"}
        else ("tenant", "quote")
    )
    with pytest.raises(Exception) as error:
        await fn(*args, actor_id="actor")
    assert getattr(error.value, "code", None) == "dependency_unavailable"
    actors.read_current.assert_not_awaited()
    repo.get.assert_not_awaited()


@pytest.mark.parametrize("reason", ["scope", "inactive", "tenant", "actor_missing"])
async def test_denial_precedes_artifact_lookup(reason):
    c = await file_case()
    if reason == "scope":
        c.scope.denied = True
    elif reason == "actor_missing":
        c.actors.read_current.return_value = None
    else:
        c.actors.read_current.return_value = c.actor.model_copy(
            update={"is_active": False}
            if reason == "inactive"
            else {"tenant_id": new_id("tn")}
        )
    with pytest.raises(PermissionDenied) as error:
        await c.service.record_file(
            c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
        )
    assert error.value.code == "permission_denied"
    c.reader.read.assert_not_awaited()
    assert not c.files


async def test_record_is_idempotent_and_metadata_precedes_write_lock():
    c = await file_case()
    first = await c.service.record_file(
        c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
    )
    second = await c.service.record_file(
        c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
    )
    assert first == second and len(c.files) == 1
    assert first.content_hash == c.meta.artifact_hash
    assert first.customer_content_hash == public.customer_quote_hash(
        public.project_customer(c.store["quote"])
    )
    assert c.order[:3] == ["scope", "metadata", "quote_lock"]
    assert c.order.index("commit") < c.order.index("release")
    assert (
        await c.service.get_file(c.tenant, QUOTE, first.file_id, actor_id=c.actor_id)
        == first
    )
    assert await c.service.list_files(c.tenant, QUOTE, actor_id=c.actor_id) == (first,)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("kind", "email_draft", "metadata_mismatch"),
        ("mime_type", "text/plain", "metadata_mismatch"),
        ("subject_ref", "quo_other", "metadata_mismatch"),
        ("sequence_number", 2, "metadata_mismatch"),
        ("workflow_run_id", "run_01K00000000000000000000001", "metadata_mismatch"),
        ("tenant_id", "tn_01K00000000000000000000001", "metadata_mismatch"),
        ("artifact_id", "art_01K00000000000000000000001", "metadata_mismatch"),
        ("idempotency_key", "wrong", "metadata_mismatch"),
        ("generated_by", "unregistered", "template_unsupported"),
    ],
)
async def test_record_rejects_real_metadata_mismatch(field, value, code):
    c = await file_case()
    c.reader.fact = c.meta.model_copy(update={field: value})
    with pytest.raises(Exception) as error:
        await c.service.record_file(
            c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
        )
    assert getattr(error.value, "code", None) == code
    assert not c.files


@pytest.mark.parametrize(
    "target", ["facts_hash", "binding", "request_payload", "run", "receipt_run"]
)
async def test_historical_receipt_checks_original_request_and_real_run(target):
    c = await file_case()
    if target == "facts_hash":
        c.store["receipt"] = c.store["receipt"].model_copy(
            update={"facts_hash": "f" * 64}
        )
    elif target == "binding":
        c.store["bindings"] = (
            c.store["bindings"][0].model_copy(update={"request_hash": "f" * 64}),
            *c.store["bindings"][1:],
        )
    elif target == "request_payload":
        c.store["bindings"] = ()
    elif target == "run":
        c.runs.read.return_value = c.runs.read.return_value.model_copy(
            update={"workflow_type": "other"}
        )
    else:
        c.store["receipt"] = c.store["receipt"].model_copy(
            update={"approval_run_id": new_id("run")}
        )
    with pytest.raises(Exception) as error:
        await c.service.get_file_approval(c.tenant, QUOTE, actor_id=c.actor_id)
    assert getattr(error.value, "code", None) in {
        "storage_inconsistent",
        "workflow_binding_invalid",
    }
    c.reader.read.assert_not_awaited()


async def test_no_receipt_is_none_for_history_and_missing_for_record():
    c = await file_case()
    c.store["receipt"] = None
    assert (
        await c.service.get_file_approval(c.tenant, QUOTE, actor_id=c.actor_id) is None
    )
    with pytest.raises(Exception) as error:
        await c.service.record_file(
            c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
        )
    assert error.value.code == "approval_missing"
    c.reader.read.assert_not_awaited()


@pytest.mark.parametrize(
    "field", ["content_hash", "customer_content_hash", "quote_content_hash"]
)
async def test_stored_hash_corruption_is_not_returned(field):
    c = await file_case()
    view = await c.service.record_file(
        c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
    )
    c.files[0] = c.files[0].model_copy(
        update={"view": view.model_copy(update={field: "f" * 64})}
    )
    for method, args in [
        (c.service.get_file, (view.file_id,)),
        (c.service.list_files, ()),
    ]:
        with pytest.raises(Exception) as error:
            await method(c.tenant, QUOTE, *args, actor_id=c.actor_id)
        assert error.value.code == "storage_inconsistent"


@pytest.mark.parametrize(
    "actor", ["", " emp_test", "emp_test ", True, "x" * 41, "emp_\x80bad", "emp_\nbad"]
)
async def test_actor_identity_rejects_noncanonical_legacy_values(actor):
    c = await file_case()
    with pytest.raises(Exception) as error:
        await c.service.list_files(c.tenant, QUOTE, actor_id=actor)
    assert error.value.code == "invalid_input"
    c.actors.read_current.assert_not_awaited()
    c.reader.read.assert_not_awaited()


async def test_legacy_employee_identity_uses_real_reader_and_scope():
    c = await file_case()
    c.actors.read_current.return_value = c.actor.model_copy(
        update={"employee_id": "emp_test"}
    )
    c.scope.expected = (c.tenant, c.store["quote"].content.opportunity_id, "emp_test")
    fact = await c.service.get_file_approval(c.tenant, QUOTE, actor_id="emp_test")
    assert fact.approval_run_id == RUN
    assert c.order[0] == "scope"


@pytest.mark.parametrize(
    "role", ["boss", "product", "sourcing", "finance", "sales", "manager"]
)
async def test_cost_roles_do_not_bypass_controlled_file_scope(role):
    c = await file_case()
    c.actors.read_current.return_value = c.actor.model_copy(update={"role": role})
    c.scope.denied = True
    with pytest.raises(PermissionDenied):
        await c.service.get_file_approval(c.tenant, QUOTE, actor_id=c.actor_id)
    c.reader.read.assert_not_awaited()


async def test_scope_authorized_manager_uses_file_purpose_not_internal_read_policy():
    c = await file_case()
    c.actors.read_current.return_value = c.actor.model_copy(update={"role": "manager"})
    assert (
        await c.service.get_file_approval(c.tenant, QUOTE, actor_id=c.actor_id)
    ).approval_run_id == RUN


@pytest.mark.parametrize(
    "dependency", ["_scope", "_artifacts", "_runs", "_actors", "_uows"]
)
async def test_missing_dependency_does_not_default_allow(dependency):
    c = await file_case()
    setattr(c.service, dependency, None)
    with pytest.raises(Exception) as error:
        await c.service.list_files(c.tenant, QUOTE, actor_id=c.actor_id)
    assert error.value.code == "dependency_unavailable"
    c.reader.read.assert_not_awaited()


async def test_metadata_failure_and_cancellation_never_fallback_or_insert():
    import asyncio

    c = await file_case()
    c.reader.read.side_effect = RuntimeError("private object address")
    with pytest.raises(Exception) as error:
        await c.service.record_file(
            c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
        )
    assert error.value.code == "dependency_unavailable"
    assert "private object address" not in str(error.value)
    c.reader.read.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await c.service.record_file(
            c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
        )
    assert c.scope.depth == 0 and not c.files


async def test_missing_file_and_artifact_use_fixed_not_found():
    c = await file_case()
    c.reader.fact = None
    for method, args in [
        (c.service.record_file, (c.meta.artifact_id,)),
        (c.service.get_file, (new_id("qfl"),)),
    ]:
        with pytest.raises(Exception) as error:
            await method(c.tenant, QUOTE, *args, actor_id=c.actor_id)
        assert error.value.code == "not_found"


async def test_existing_file_cannot_be_overwritten_by_other_artifact_metadata():
    c = await file_case()
    first = await c.service.record_file(
        c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
    )
    c.reader.fact = c.meta.model_copy(
        update={"artifact_hash": "e" * 64, "size_bytes": 8}
    )
    with pytest.raises(Exception) as error:
        await c.service.record_file(
            c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
        )
    assert error.value.code == "file_conflict" and c.files[0].view == first


async def test_stored_file_with_corrupt_run_maps_to_storage_inconsistent():
    c = await file_case()
    view = await c.service.record_file(
        c.tenant, QUOTE, c.meta.artifact_id, actor_id=c.actor_id
    )
    c.runs.read.return_value = c.runs.read.return_value.model_copy(
        update={"content_hash": "f" * 64}
    )
    with pytest.raises(Exception) as error:
        await c.service.get_file(c.tenant, QUOTE, view.file_id, actor_id=c.actor_id)
    assert error.value.code == "storage_inconsistent"
