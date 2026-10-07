"""真实T4创建→T5审批receipt→T6 Store，文件actor不冒充审批人。"""

# ruff: noqa: PLC0414 -- pytest真实fixture显式重导出

import asyncio
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from domains.quotations.customer_versions import QuoteCustomerVersionsServiceImpl
from domains.quotations.file_access import (
    ContextQuoteFileScopeAuthorizer,
    QuoteFileAccessServiceImpl,
)
from infra.db.tables import EmployeeRow
from shared.schemas.quote_document import customer_quote_hash
from tests.integration.test_quote_pdf_artifacts import (
    approval_case as approval_case,
)
from tests.integration.test_quote_pdf_artifacts import (
    approved_file_case as approved_file_case,
)
from tests.integration.test_quote_pdf_artifacts import (
    context_case as context_case,
)
from tests.integration.test_quote_pdf_artifacts import (
    file_service_for,
)
from tests.integration.test_quote_pdf_artifacts import (
    freeze_case as freeze_case,
)
from tests.integration.test_quote_pdf_artifacts import (
    prepared_quote as prepared_quote,
)
from tests.integration.test_quote_pdf_artifacts import (
    quotation_case as quotation_case,
)
from tests.integration.test_quote_pdf_artifacts import (
    recovery_case as recovery_case,
)
from tests.integration.test_quote_pdf_artifacts import (
    unit_db_case as unit_db_case,
)
from tests.integration.test_quote_pdf_artifacts import (
    unit_engine as unit_engine,
)
from workflows.quote_approval.file_facts import (
    ApprovalServiceQuoteFileFactsReader,
    DemandQuoteFileNeedValidator,
)


@pytest_asyncio.fixture
async def file_access_case(approved_file_case):
    return build_file_access_case(approved_file_case)


def build_file_access_case(c):
    """仅复用真实依赖装配；批准/receipt仍由各fixture实际执行。"""
    c.scope = ContextQuoteFileScopeAuthorizer(c.approval.provider)
    c.files = file_service_for(c)
    c.access = QuoteFileAccessServiceImpl(
        c.approval.quotation.factory,
        c.approval.quotation.actors,
        c.approval.provider,
        ApprovalServiceQuoteFileFactsReader(c.approval.approvals),
        DemandQuoteFileNeedValidator(),
        c.approval.policies,
        c.runs,
        c.files,
        now=lambda: c.approval.quotation.clock[0],
        template_version="quote_pdf_v1",
    )
    c.sales_id = c.quote.content.owner_id
    c.quote_id = c.quote.content.quote_id
    c.pages = QuoteCustomerVersionsServiceImpl(
        c.approval.quotation.factory,
        c.approval.provider,
        c.access,
        c.files,
        now=lambda: c.approval.quotation.clock[0],
        maximum_page_size=10,
    )
    return c


@pytest_asyncio.fixture
async def mixed_decider_file_case(approval_case, unit_engine):
    from types import SimpleNamespace

    from tests.integration.test_quote_approval_postgres import bind_real_approvals
    from tests.integration.test_quote_pdf_artifacts import (
        MemoryBlobs,
        pdf_args,
        store_for,
    )
    from workflows.quote_approval.approvals import read_quote_facts
    from workflows.quote_approval.run_reader import WorkflowQuoteRunReader
    c = approval_case
    await c.quotation.context.update_owner_manager()
    ids = await bind_real_approvals(c)
    pending = await read_quote_facts(c.approvals, c.quotation.tenant, ids)
    assert len(pending) >= 2
    manager = "emp_manager"
    for fact in pending:
        await c.approvals.decide(c.quotation.tenant, fact.approval_id, approved=True,
            decided_by=c.decider if fact.approval_type == "quote_send" else manager)
    facts = await read_quote_facts(c.approvals, c.quotation.tenant, ids)
    async with (
        c.provider.open_for_approval(c.quotation.tenant, c.quote.content.opportunity_id, c.decider,
            prepared_by=c.quote.content.prepared_by, decider_ids=(manager, c.decider)) as current,
        c.quotation.service.open_approval(c.quotation.tenant, c.quote.content.quote_id, executor=c.executor) as session,
    ):
        result = await session.apply(facts, current)
    blobs = MemoryBlobs()
    store = store_for(unit_engine, blobs)
    quote = result.quote
    args = pdf_args() | {"tenant_id": c.quotation.tenant, "subject_ref": quote.content.quote_id,
        "sequence_number": quote.content.version, "workflow_run_id": result.receipt.approval_run_id,
        "idempotency_key": f"{quote.content.quote_id}:{quote.content.version}:quote_pdf:quote_pdf_v1"}
    meta = await store.put(**args)
    return build_file_access_case(SimpleNamespace(approval=c, quote=quote, receipt=result.receipt,
        meta=meta, store=store, blobs=blobs, runs=WorkflowQuoteRunReader(lambda: c.engine),
        tenant=c.quotation.tenant, actor_id=c.quotation.actor.employee_id, engine=unit_engine,
        independent_decider=manager))


@pytest.mark.parametrize("field,value", [("is_active", False), ("role", "finance")])
async def test_independent_non_send_decider_revocation_blocks_new_file_service(mixed_decider_file_case, field, value):
    c = mixed_decider_file_case
    assert c.independent_decider != c.receipt.quote_send_decider
    assert any(f.approval_type != "quote_send" and f.decided_by == c.independent_decider for f in c.receipt.decisions)
    await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    file = await c.files.record_file(c.tenant, c.quote_id, c.meta.artifact_id, actor_id=c.sales_id)
    await c.approval.quotation.context.update_employee(c.independent_decider, **{field: value})
    with pytest.raises(Exception) as error:
        await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    assert error.value.code == "decider_invalid"
    assert await c.access.authorize_history(c.tenant, c.quote_id, file.file_id, actor_id=c.sales_id) == file
    assert c.blobs.gets == 0


async def test_customer_versions_real_limit_plus_one_and_continuous_pages(file_access_case):
    from tests.integration.test_quotation_creation_recovery import new_command
    c = file_access_case
    original = await c.files.record_file(c.tenant, c.quote_id, c.meta.artifact_id, actor_id=c.sales_id)
    latest = c.quote
    for version in (2, 3):
        key = f"file-page-revision-{version}"
        command = await new_command(c.approval.creation, key, previous=latest)
        latest = await c.approval.creation.app.create(c.tenant, command,
            actor_id=c.approval.quotation.actor.employee_id, idempotency_key=key)
        assert latest.content.version == version
    before = None
    observed = []
    for expected in (3, 2, 1):
        page = await c.pages.list_versions(c.tenant, c.quote.content.opportunity_id, actor_id=c.sales_id,
            before_version=before, limit=1)
        assert [item.version for item in page.items] == [expected]
        assert page.next_before_version == (expected if expected > 1 else None)
        observed.extend(item.quote_id for item in page.items)
        before = page.next_before_version
        if expected == 1:
            assert page.items[0].files[0].file == original
            assert page.items[0].files[0].allowed_actions == ("read_history",)
    assert len(set(observed)) == 3
    empty = await c.pages.list_versions(c.tenant, c.quote.content.opportunity_id, actor_id=c.sales_id,
        before_version=1, limit=1)
    assert empty.items == () and empty.next_before_version is None


async def test_file_actor_is_not_send_decider(file_access_case):
    c = file_access_case
    assert c.sales_id != c.receipt.quote_send_decider
    snapshot = await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    assert snapshot.approval_run_id == c.receipt.approval_run_id
    assert snapshot.customer_content_hash == customer_quote_hash(snapshot.customer)
    assert c.blobs.gets == 0


@asynccontextmanager
async def held_file_business_lock(c, target):
    """真实独立连接持锁，不修改员工/报价/政策以伪造等待。"""
    q = c.approval.quotation
    if target == "employee":
        async with q.context.unit.sessions.begin() as session:
            await session.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == c.tenant,
                EmployeeRow.employee_id == c.receipt.quote_send_decider).with_for_update())
            yield
    elif target == "quotation":
        async with q.factory(c.tenant) as uow:
            await uow.quotes.lock_opportunity(c.tenant, c.quote.content.opportunity_id)
            yield
    else:
        async with c.approval.creation.freeze.factory(c.tenant) as uow:
            await uow.policies.lock_selection(c.tenant, exclusive=True)
            yield


@pytest.mark.parametrize("waiting_on", ["employee", "quotation", "policy"])
async def test_formal_issuer_is_rechecked_after_every_business_wait(file_access_case, monkeypatch, waiting_on):
    c = file_access_case
    selected = asyncio.Event()
    reader = c.approval.provider._issuer
    original = reader.get_confirmed
    async def observed(tenant):
        issuer = await original(tenant)
        selected.set()
        return issuer
    monkeypatch.setattr(reader, "get_confirmed", observed)
    task = None
    try:
        async with held_file_business_lock(c, waiting_on):
            task = asyncio.create_task(c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id))
            await asyncio.wait_for(selected.wait(), 2)
            await c.approval.quotation.context.wait_for_blocked_writer(task)
            changed = await c.approval.quotation.issuer(key="issuer-during-file-wait", name="Changed Supplier")
            assert changed != c.quote.content.issuer and not task.done()
        with pytest.raises(Exception) as error:
            await task
        assert error.value.code == "context_changed"
        assert c.blobs.gets == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("fault,expected", [("missing", "context_changed"), ("unknown", "dependency_unavailable"), ("corrupt", "storage_inconsistent")])
async def test_issuer_reader_failure_keeps_business_and_technical_classes(file_access_case, monkeypatch, fault, expected):
    from domains.quotations.errors import QuotationError, QuotationUnavailableError
    c = file_access_case
    async def failed(tenant):
        assert tenant == c.tenant
        if fault == "missing": raise QuotationError("issuer_not_found")
        if fault == "corrupt": raise QuotationUnavailableError("storage_inconsistent")
        raise RuntimeError("private-reader-marker")
    monkeypatch.setattr(c.approval.provider._issuer, "get_confirmed", failed)
    with pytest.raises(Exception) as error:
        await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    assert error.value.code == expected and "private-reader-marker" not in str(error.value)
    if fault == "missing":
        page = await c.pages.list_versions(c.tenant, c.quote.content.opportunity_id, actor_id=c.sales_id, before_version=None, limit=1)
        assert {b.code for b in page.items[0].blockers} == {"context_changed"}
    else:
        with pytest.raises(Exception) as page_error:
            await c.pages.list_versions(c.tenant, c.quote.content.opportunity_id, actor_id=c.sales_id, before_version=None, limit=1)
        assert page_error.value.code == expected
    assert c.blobs.gets == 0


async def test_real_missing_issuer_reader_has_file_only_classification(quotation_case):
    from domains.quotations.errors import (
        QuoteContextError,
        QuoteContextUnavailableError,
    )
    from infra.db.quote_context import SqlAlchemyQuoteContextProvider
    from workflows.quote_approval.issuer_reader import PersistentQuoteIssuerReader

    c = quotation_case
    provider = SqlAlchemyQuoteContextProvider(c.context.unit.sessions,
        PersistentQuoteIssuerReader(c.service), lock_timeout_ms=1000, statement_timeout_ms=2500)
    async with c.factory(c.tenant) as uow:
        assert await uow.quotes.current_issuer(c.tenant) is None
    with pytest.raises(QuoteContextError) as missing:
        async with provider.open_for_file(c.tenant, c.context.opportunity_id, "emp_owner",
            prepared_by=c.actor.employee_id, decider_ids=(c.actor.employee_id,)):
            pytest.fail("缺失真实持久抬头不得形成正式context")
    assert missing.value.code == "context_changed"
    with pytest.raises(QuoteContextUnavailableError) as legacy:
        async with provider.open(c.tenant, c.context.opportunity_id, c.actor.employee_id,
            prepared_by=c.actor.employee_id):
            pytest.fail("旧prepare/apply的错误分类保持原契约")
    assert legacy.value.code == "dependency_unavailable"
    async with provider.open_file_scope(c.tenant, c.context.opportunity_id, "emp_owner") as facts:
        assert facts.actor.employee_id == "emp_owner"


async def test_final_issuer_wait_rechecks_policy_and_clock(file_access_case, monkeypatch):
    from workflows.quote_approval.policy_reader import _Selection

    c = file_access_case
    original = _Selection.current
    checked = []
    async def observed(selection):
        checked.append(c.approval.quotation.clock[0])
        return await original(selection)
    monkeypatch.setattr(_Selection, "current", observed)
    async with c.approval.quotation.factory(c.tenant) as uow:
        await uow.quotes.lock_issuer(c.tenant)
        task = asyncio.create_task(c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id))
        await c.approval.quotation.context.wait_for_blocked_writer(task)
        assert checked == [] and not task.done()
        c.approval.quotation.clock[0] = c.quote.content.valid_until
    with pytest.raises(Exception) as error:
        await task
    assert error.value.code == "quote_expired"
    assert checked == [c.quote.content.valid_until] and c.blobs.gets == 0


async def test_final_issuer_lock_is_held_until_readonly_authorization_exits(file_access_case, monkeypatch):
    from workflows.quote_approval.policy_reader import _Selection

    c = file_access_case
    entered, release = asyncio.Event(), asyncio.Event()
    original = _Selection.current
    async def observed(selection):
        result = await original(selection)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(_Selection, "current", observed)
    task = asyncio.create_task(c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id))
    writer = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        writer = asyncio.create_task(c.approval.quotation.issuer(key="issuer-after-formal", name="Next Supplier"))
        await c.approval.quotation.context.wait_for_blocked_writer(writer)
        assert not writer.done()
        release.set()
        snapshot = await task
        assert snapshot.quote_id == c.quote_id
        await asyncio.wait_for(writer, 3)
        with pytest.raises(Exception) as changed:
            await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
        assert changed.value.code == "context_changed" and c.blobs.gets == 0
    finally:
        release.set()
        for pending in (task, writer):
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_final_issuer_wait_timeout_or_cancel_releases_all_business_locks(file_access_case, cancel):
    c = file_access_case
    task = None
    try:
        async with c.approval.quotation.factory(c.tenant) as uow:
            await uow.quotes.lock_issuer(c.tenant)
            task = asyncio.create_task(c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id))
            await c.approval.quotation.context.wait_for_blocked_writer(task)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(Exception) as error:
                    await task
                assert error.value.code == "lock_timeout"
        await asyncio.wait_for(c.approval.quotation.context.update_owner_manager(), 2)
        assert (await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)).quote_id == c.quote_id
        assert c.blobs.gets == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_final_issuer_lock_is_tenant_scoped(file_access_case):
    from shared.schemas.identifiers import new_id

    c = file_access_case
    foreign = new_id("tn")
    async with c.approval.quotation.factory(foreign) as uow:
        await uow.quotes.lock_issuer(foreign)
        snapshot = await asyncio.wait_for(c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id), 2)
        assert snapshot.tenant_id == c.tenant
    with pytest.raises(Exception) as error:
        await c.access.authorize(foreign, c.quote_id, actor_id=c.sales_id)
    assert error.value.code in {"permission_denied", "not_found"} and c.blobs.gets == 0


async def test_final_issuer_cancel_survives_context_rollback_failure(file_access_case, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    c = file_access_case
    original = AsyncSession.rollback
    async def failed(session):
        await original(session)
        raise SQLAlchemyError("private-cancel-cleanup-marker")
    async with c.approval.quotation.factory(c.tenant) as uow:
        await uow.quotes.lock_issuer(c.tenant)
        task = asyncio.create_task(c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id))
        await c.approval.quotation.context.wait_for_blocked_writer(task)
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "rollback", failed)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    await asyncio.wait_for(c.approval.quotation.context.update_owner_manager(), 2)
    assert (await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)).quote_id == c.quote_id
    assert c.blobs.gets == 0


@pytest.mark.parametrize("purpose,waiting_on,fault", [
    (purpose, waiting_on, fault)
    for purpose in ("scope", "current")
    for waiting_on in ("employee", "opportunity", "consumer")
    for fault in ("rollback", "close", "both")
] + [(purpose, "consumer", fault) for purpose in ("scope", "current")
    for fault in ("validation", "secondary_cancel")])
async def test_file_context_preserves_primary_cancel_across_cleanup(
    file_access_case, monkeypatch, caplog, purpose, waiting_on, fault
):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    from infra.db.tables import OpportunityRow

    c = file_access_case
    execute, rollback, close = AsyncSession.execute, AsyncSession.rollback, AsyncSession.close
    entered = asyncio.Event()
    task, target, enabled = None, None, False
    primary, failures = [], []

    async def observed(session, *args, **kwargs):
        nonlocal target
        if asyncio.current_task() is task:
            target = session
        try:
            return await execute(session, *args, **kwargs)
        except asyncio.CancelledError as error:
            if session is target:
                primary.append(error)
            raise

    async def failed_rollback(session):
        await rollback(session)
        if enabled and session is target and fault in {"rollback", "both"}:
            failures.append("rollback")
            raise SQLAlchemyError("private-rollback-marker")

    async def failed_close(session):
        await close(session)
        if enabled and session is target and fault in {"close", "both", "validation", "secondary_cancel"}:
            failures.append("close")
            if fault == "validation":
                raise ValueError("private-validation-marker")
            if fault == "secondary_cancel":
                raise asyncio.CancelledError("private-secondary-cancel-marker")
            raise SQLAlchemyError("private-close-marker")

    async def consume():
        args = (c.tenant, c.quote.content.opportunity_id, c.sales_id)
        lease = (c.approval.provider.open_file_scope(*args) if purpose == "scope" else
            c.approval.provider.open_for_file(*args, prepared_by=c.quote.content.prepared_by,
                decider_ids=tuple({d.decided_by for d in c.receipt.decisions})))
        async with lease:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                primary.append(error)
                raise

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", observed)
        patch.setattr(AsyncSession, "rollback", failed_rollback)
        patch.setattr(AsyncSession, "close", failed_close)
        try:
            async with c.approval.quotation.context.unit.sessions.begin() as locker:
                if waiting_on == "employee":
                    await locker.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == c.tenant,
                        EmployeeRow.employee_id == c.sales_id).with_for_update())
                elif waiting_on == "opportunity":
                    await locker.scalar(select(OpportunityRow).where(OpportunityRow.tenant_id == c.tenant,
                        OpportunityRow.opportunity_id == c.quote.content.opportunity_id).with_for_update())
                task = asyncio.create_task(consume())
                if waiting_on == "consumer":
                    await asyncio.wait_for(entered.wait(), 2)
                else:
                    await c.approval.quotation.context.wait_for_blocked_writer(task)
                    assert not entered.is_set()
                enabled = True
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as error:
                    await task
                assert len(primary) == 1 and error.value is primary[0]
                expected = ["rollback", "close"] if fault == "both" else [
                    "rollback" if fault == "rollback" else "close"]
                assert failures == expected and "private-" not in caplog.text
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(c.approval.quotation.context.update_owner_manager(), 2)
    assert (await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)).quote_id == c.quote_id
    assert c.blobs.gets == 0


@pytest.mark.parametrize("purpose", ["scope", "current"])
@pytest.mark.parametrize("stage", ["bootstrap", "rollback", "close"])
async def test_file_context_first_cancel_at_session_boundary(file_access_case, monkeypatch, purpose, stage):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    c = file_access_case
    execute, rollback, close, exiting = (
        AsyncSession.execute, AsyncSession.rollback, AsyncSession.close, AsyncSession.__aexit__
    )
    entered, release = asyncio.Event(), asyncio.Event()
    task, target, main_ready, armed = None, None, False, False
    primary, failures = [], []

    async def pause():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError as error:
            primary.append(error)
            raise

    async def observed(session, *args, **kwargs):
        nonlocal target
        result = await execute(session, *args, **kwargs)
        if asyncio.current_task() is task:
            target = session
            if stage == "bootstrap":
                await pause()
        return result

    async def observed_rollback(session):
        await rollback(session)
        if stage == "rollback" and main_ready and session is target:
            await pause()

    async def observed_close(session):
        await close(session)
        if stage == "close" and main_ready and session is target:
            entered.set()
            await release.wait()
        elif armed and session is target:
            failures.append("close")
            raise SQLAlchemyError("private-close-boundary-marker")

    async def observed_exit(session, *args):
        try:
            await exiting(session, *args)
        except asyncio.CancelledError as error:
            if stage == "close" and session is target:
                primary.append(error)
            raise

    async def consume():
        nonlocal main_ready
        args = (c.tenant, c.quote.content.opportunity_id, c.sales_id)
        lease = (c.approval.provider.open_file_scope(*args) if purpose == "scope" else
            c.approval.provider.open_for_file(*args, prepared_by=c.quote.content.prepared_by,
                decider_ids=tuple({d.decided_by for d in c.receipt.decisions})))
        async with lease:
            main_ready = True

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", observed)
        patch.setattr(AsyncSession, "rollback", observed_rollback)
        patch.setattr(AsyncSession, "close", observed_close)
        patch.setattr(AsyncSession, "__aexit__", observed_exit)
        try:
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 2)
            armed = True
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert len(primary) == 1 and error.value is primary[0]
            assert failures == ([] if stage == "close" else ["close"])
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(c.approval.quotation.context.update_owner_manager(), 2)
    assert (await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)).quote_id == c.quote_id
    assert c.blobs.gets == 0


@pytest.mark.parametrize("purpose", ["scope", "current"])
@pytest.mark.parametrize("signal_type", [SystemExit, KeyboardInterrupt, GeneratorExit])
@pytest.mark.parametrize("later", ["sql", "cancel", "terminal"])
async def test_yielded_file_context_keeps_first_terminal(file_access_case, monkeypatch, caplog, purpose, signal_type, later):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.unit.test_quote_file_access import ControlledCleanupSignal

    c = file_access_case
    signal = signal_type("private-yield-terminal")
    subsequent = {"sql": SQLAlchemyError("private-rollback-sql"),
        "cancel": asyncio.CancelledError("private-rollback-cancel"),
        "terminal": ControlledCleanupSignal("private-second-terminal")}[later]
    execute, rollback, close = AsyncSession.execute, AsyncSession.rollback, AsyncSession.close
    owner = asyncio.current_task()
    target, armed = None, False
    cleanup = []

    async def observed(session, *args, **kwargs):
        nonlocal target
        result = await execute(session, *args, **kwargs)
        if asyncio.current_task() is owner:
            target = session
        return result

    async def failed_rollback(session):
        await rollback(session)
        if armed and session is target:
            cleanup.append("rollback")
            raise subsequent

    async def failed_close(session):
        await close(session)
        if armed and session is target:
            cleanup.append("close")
            raise SQLAlchemyError("private-final-close")

    args = (c.tenant, c.quote.content.opportunity_id, c.sales_id)
    lease = (c.approval.provider.open_file_scope(*args) if purpose == "scope" else
        c.approval.provider.open_for_file(*args, prepared_by=c.quote.content.prepared_by,
            decider_ids=tuple({d.decided_by for d in c.receipt.decisions})))
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", observed)
        patch.setattr(AsyncSession, "rollback", failed_rollback)
        patch.setattr(AsyncSession, "close", failed_close)
        with pytest.raises(signal_type) as error:
            async with lease:
                armed = True
                raise signal
        assert error.value is signal and cleanup == ["rollback", "close"]
        assert "private-" not in caplog.text
    await asyncio.wait_for(c.approval.quotation.context.update_owner_manager(), 2)
    assert (await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)).quote_id == c.quote_id
    assert c.blobs.gets == 0


@pytest.mark.parametrize("fault", ["rollback", "close", "both"])
async def test_formal_context_cleanup_failure_cannot_return_snapshot(file_access_case, monkeypatch, fault):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    from workflows.quote_approval.policy_reader import _Selection

    c = file_access_case
    checked = False
    current, rollback, close = _Selection.current, AsyncSession.rollback, AsyncSession.close
    async def observed(selection):
        nonlocal checked
        result = await current(selection)
        checked = True
        return result
    async def failed(session):
        await rollback(session)
        if checked and fault in {"rollback", "both"}:
            raise SQLAlchemyError("private-cleanup-marker")
    async def failed_close(session):
        await close(session)
        if checked and fault in {"close", "both"}:
            raise SQLAlchemyError("private-cleanup-close-marker")
    with monkeypatch.context() as patch:
        patch.setattr(_Selection, "current", observed)
        patch.setattr(AsyncSession, "rollback", failed)
        patch.setattr(AsyncSession, "close", failed_close)
        with pytest.raises(Exception) as error:
            await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
        assert error.value.code == "dependency_unavailable"
        assert "private-cleanup-marker" not in str(error.value)
    assert checked and c.blobs.gets == 0
    await asyncio.wait_for(c.approval.quotation.context.update_owner_manager(), 2)
    assert (await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)).quote_id == c.quote_id


@pytest.mark.parametrize(
    "field,value,code",
    [("is_active", False, "decider_invalid"), ("role", "finance", "decider_invalid")],
)
async def test_current_decider_revocation_blocks_formal_not_history(
    file_access_case, field, value, code
):
    c = file_access_case
    file = await c.files.record_file(
        c.tenant, c.quote_id, c.meta.artifact_id, actor_id=c.sales_id
    )
    async with c.approval.quotation.context.unit.sessions.begin() as session:
        await session.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == c.tenant,
                EmployeeRow.employee_id == c.receipt.quote_send_decider,
            )
            .values(**{field: value})
        )
    with pytest.raises(Exception) as error:
        await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    chain = []
    nested = error.value
    while nested is not None:
        chain.append((type(nested).__name__, getattr(nested, "code", None)))
        nested = nested.__context__
    assert error.value.code == code, chain
    assert (
        await c.access.authorize_history(
            c.tenant, c.quote_id, file.file_id, actor_id=c.sales_id
        )
        == file
    )


async def test_customer_page_has_no_internal_quote_and_expiry_does_not_write_state(
    file_access_case,
):
    c = file_access_case
    file = await c.files.record_file(
        c.tenant, c.quote_id, c.meta.artifact_id, actor_id=c.sales_id
    )
    c.approval.quotation.clock[0] = c.quote.content.valid_until
    page = await c.pages.list_versions(
        c.tenant,
        c.quote.content.opportunity_id,
        actor_id=c.sales_id,
        before_version=None,
        limit=1,
    )
    item = page.items[0]
    assert item.state.value == "approved" and item.is_past_valid_until
    assert item.allowed_actions == () and item.files[0].allowed_actions == (
        "read_history",
    )
    assert item.files[0].file == file and page.next_before_version is None
    assert {b.code for b in item.blockers} == {"quote_expired"}
    assert not {
        "basis",
        "cost",
        "profit",
        "customer",
        "need_facts",
        "provenance",
        "payload",
    } & set(item.model_dump())


@pytest.mark.parametrize("field", ["quantity", "material", "required_by"])
async def test_current_need_change_invalidates_formal_only(file_access_case, field):
    c = file_access_case
    value = {"quantity": 600, "material": "aluminium", "required_by": "2026-09-15"}[
        field
    ]
    ctx = c.approval.quotation.context
    await ctx.unit.demand.update_need_fields(
        c.tenant,
        ctx.unit.need_id,
        {field: {"value": value, "quote": str(value), "extracted_by": ctx.actor_id}},
        source_message_id="msg_file_changed",
        updated_by=ctx.actor_id,
    )
    with pytest.raises(Exception) as error:
        await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    assert error.value.code == "context_changed"
    assert c.blobs.gets == 0


async def test_completed_run_and_mixed_applied_facts_keep_real_receipt(
    file_access_case,
):
    c = file_access_case
    await c.approval.approvals.mark_applied(
        c.tenant, c.receipt.decisions[0].approval_id, "controlled-application"
    )
    assert await c.approval.engine.poll_due(c.tenant, limit=1) == 1
    snapshot = await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    assert snapshot.approval_facts_hash == c.receipt.facts_hash


@pytest.mark.parametrize("target", ["actor", "owner", "decider"])
async def test_file_lease_holds_all_employees_before_opportunity(context_case, target):
    c = context_case
    actor, decider, owner = "emp_owner", c.actor_id, "emp_owner"
    employee = {"actor": actor, "owner": owner, "decider": decider}[target]
    async with c.provider.open_for_file(
        c.tenant_id,
        c.opportunity_id,
        actor,
        prepared_by=c.prepared_by,
        decider_ids=(decider,),
    ) as facts:
        assert (
            facts.business.runtime.current_actor.employee_id
            != facts.deciders[0].employee_id
        )
        writer = asyncio.create_task(c.update_employee(employee, is_active=False))
        await c.wait_for_blocked_writer(writer)
        assert not writer.done()
    await asyncio.wait_for(writer, 3)


async def test_scope_has_no_need_or_issuer_dependency(context_case):
    c = context_case

    class NoIssuer:
        async def get_confirmed(self, tenant):
            pytest.fail("历史scope不得读issuer")

    c.provider._issuer = NoIssuer()
    async with c.provider.open_file_scope(
        c.tenant_id, c.opportunity_id, "emp_owner"
    ) as facts:
        assert facts.actor.employee_id == "emp_owner"
        writer = asyncio.create_task(c.update_owner_manager())
        await c.wait_for_blocked_writer(writer)
    await asyncio.wait_for(writer, 3)


@pytest.mark.parametrize("actor", ["emp_missing", "bad actor\n", ""])
async def test_invalid_or_missing_actor_stops_before_approval_and_objects(
    file_access_case, actor
):
    c = file_access_case

    class NoFacts:
        async def read(self, *args):
            pytest.fail("无效actor不得读取审批")

    c.access._facts = NoFacts()
    with pytest.raises(Exception) as error:
        await c.access.authorize(c.tenant, c.quote_id, actor_id=actor)
    assert error.value.code in {"permission_denied", "invalid_input"}
    assert c.blobs.gets == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_hash", "0" * 64),
        ("proposed_by_run", "run_00000000000000000000000000"),
        ("decided_by", "emp_owner"),
        ("decision_note", "different immutable decision"),
    ],
)
async def test_current_immutable_approval_mismatch_is_storage_error(
    file_access_case, field, value
):
    c = file_access_case
    original = c.access._facts

    class ChangedFacts:
        async def read(self, tenant, ids):
            facts = await original.read(tenant, ids)
            return (facts[0].model_copy(update={field: value}), *facts[1:])

    c.access._facts = ChangedFacts()
    with pytest.raises(Exception) as error:
        await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    assert error.value.code == "storage_inconsistent"


async def test_page_dependency_failure_is_not_empty_or_blocker(file_access_case):
    c = file_access_case

    class Unavailable:
        async def authorize(self, *args, **kwargs):
            raise RuntimeError("private reader detail")

    c.pages._access = Unavailable()
    with pytest.raises(Exception) as error:
        await c.pages.list_versions(
            c.tenant,
            c.quote.content.opportunity_id,
            actor_id=c.sales_id,
            before_version=None,
            limit=1,
        )
    assert error.value.code == "dependency_unavailable"
    assert "private" not in str(error.value)


async def test_before_version_excludes_current_without_empty_cursor(file_access_case):
    c = file_access_case
    page = await c.pages.list_versions(
        c.tenant,
        c.quote.content.opportunity_id,
        actor_id=c.sales_id,
        before_version=1,
        limit=1,
    )
    assert page.items == () and page.next_before_version is None


async def test_history_keeps_real_scope_lock_timeout(file_access_case):
    c = file_access_case
    file = await c.files.record_file(c.tenant, c.quote_id, c.meta.artifact_id, actor_id=c.sales_id)
    c.approval.provider._lock_timeout = 20
    async with c.approval.quotation.context.unit.sessions.begin() as session:
        await session.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == c.tenant,
            EmployeeRow.employee_id == c.sales_id).values(manager_id="emp_manager"))
        with pytest.raises(Exception) as error:
            await c.access.authorize_history(c.tenant, c.quote_id, file.file_id, actor_id=c.sales_id)
        assert error.value.code == "lock_timeout"
