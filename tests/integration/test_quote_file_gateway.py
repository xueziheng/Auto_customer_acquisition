"""真实PG→T4/T5/T6/renderer/Gateway链；对象边界受控，限速本组先用受控端口。"""

# ruff: noqa: PLC0414 -- 显式复用真实fixture链
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from artifact_store.service_impl import GeneratedArtifactStoreImpl
from artifact_store.transport import BlobReadLimitExceeded
from connectors.quote_pdf.client import ReportLabQuotePdfRenderer
from domains.quotations.file_service import QuoteFileServiceImpl
from domains.quotations.service import (
    ContextQuoteFileScopeAuthorizer,
    QuoteFileAccessServiceImpl,
)
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.tables import EmployeeRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.quote_document_store import GeneratedStoreDocumentAdapter
from infra.quote_file_artifacts import GeneratedStoreQuoteArtifactReader
from shared.schemas.identifiers import new_id
from tests.integration.test_quote_approval_postgres import bind_real_approvals
from tests.integration.test_quote_file_access import (
    approval_case as approval_case,
)
from tests.integration.test_quote_file_access import (
    context_case as context_case,
)
from tests.integration.test_quote_file_access import (
    freeze_case as freeze_case,
)
from tests.integration.test_quote_file_access import (
    prepared_quote as prepared_quote,
)
from tests.integration.test_quote_file_access import (
    quotation_case as quotation_case,
)
from tests.integration.test_quote_file_access import (
    recovery_case as recovery_case,
)
from tests.integration.test_quote_file_access import (
    unit_db_case as unit_db_case,
)
from tests.integration.test_quote_file_access import (
    unit_engine as unit_engine,
)
from tests.integration.test_quote_pdf_artifacts import MemoryBlobs
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.quote_files import (
    QuoteFileApprovalCheck,
    QuoteFilePermissionCheck,
    QuoteFileRateLimitCheck,
    QuoteFileTenantCheck,
)
from tool_gateway.file_rate_limit import QuoteFileRateDecision
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.quote_files import (
    QuoteFileGenerateHandler,
    QuoteFileReadHandler,
    QuoteFileResultSlot,
    register_quote_file_tools,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway
from tool_gateway.quote_file_ledger import PublicLedgerQuoteGenerationReader
from workflows.quote_approval.approvals import read_quote_facts
from workflows.quote_approval.file_facts import (
    ApprovalServiceQuoteFileFactsReader,
    DemandQuoteFileNeedValidator,
)
from workflows.quote_approval.file_schemas import QuoteFileApplicationError
from workflows.quote_approval.files import QuoteFilesApplication
from workflows.quote_approval.run_reader import WorkflowQuoteRunReader


class Objects(MemoryBlobs):
    after_read = None

    async def get_bounded(self, key, *, maximum_bytes):
        self.gets += 1
        content = self.objects[key]
        if len(content) > maximum_bytes:
            raise BlobReadLimitExceeded()
        if self.after_read is not None:
            await self.after_read()
        return content


class Renderer:
    def __init__(self):
        self.calls = 0
        self.real = ReportLabQuotePdfRenderer(2_000_000, 20, maximum_text_bytes=100_000)

    def render(self, customer, *, template_version):
        self.calls += 1
        return self.real.render(customer, template_version=template_version)


class ControlledRate:
    async def reserve(self, tenant, request):
        return QuoteFileRateDecision(
            outcome="reserved",
            reservation_event_id=new_id("tce"),
            retry_after_seconds=None,
        )


class ControlledHistory:
    async def has_prior_execution(self, tenant, request):
        return False


class NotYetRecovery:
    async def prepare(self, ctx, preflight):
        pytest.fail("8.3不调用显式恢复；真实实现由8.5验收")

    async def execute(self, tenant, prepared):
        pytest.fail("8.3不调用显式恢复")


@pytest_asyncio.fixture
async def file_gateway_case(approval_case):
    a = approval_case
    q = a.quotation
    ids = await bind_real_approvals(a)
    for aid in ids:
        await a.approvals.decide(q.tenant, aid, approved=True, decided_by=a.decider)
    facts = await read_quote_facts(a.approvals, q.tenant, ids)
    async with (
        a.provider.open_for_approval(
            q.tenant,
            a.quote.content.opportunity_id,
            a.decider,
            prepared_by=a.quote.content.prepared_by,
            decider_ids=(a.decider,),
        ) as context,
        q.service.open_approval(
            q.tenant, a.quote.content.quote_id, executor=a.executor
        ) as session,
    ):
        approved = await session.apply(facts, context)
    sessions = q.context.unit.sessions
    async with sessions.begin() as db:
        await db.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == q.tenant,
                EmployeeRow.employee_id == a.quote.content.owner_id,
            )
            .values(manager_id="emp_manager")
        )
    objects = Objects()
    store = GeneratedArtifactStoreImpl(
        lambda tenant: SqlAlchemyArtifactUnitOfWork(sessions, tenant),
        objects,
        2_000_000,
        lambda: q.clock[0],
        new_id,
        bounded_transport=objects,
    )
    runs = WorkflowQuoteRunReader(lambda: a.engine)
    files = QuoteFileServiceImpl(
        q.factory,
        q.actors,
        ContextQuoteFileScopeAuthorizer(a.provider),
        GeneratedStoreQuoteArtifactReader(store),
        runs,
        id_generator=new_id,
    )
    access = QuoteFileAccessServiceImpl(
        q.factory,
        q.actors,
        a.provider,
        ApprovalServiceQuoteFileFactsReader(a.approvals),
        DemandQuoteFileNeedValidator(),
        a.policies,
        runs,
        files,
        now=lambda: q.clock[0],
        template_version="quote_pdf_v1",
    )
    document_store = GeneratedStoreDocumentAdapter(store, store)
    fingerprints = HmacFingerprintProvider("test-v1", b"x" * 32)
    slot, renderer = QuoteFileResultSlot(), Renderer()
    ledger_factory = lambda tenant: SqlAlchemyToolGatewayUnitOfWork(sessions, tenant)
    ledger = PublicLedgerQuoteGenerationReader(ledger_factory)
    generate = QuoteFileGenerateHandler(
        access,
        files,
        document_store,
        renderer,
        ControlledHistory(),
        fingerprints,
        slot,
        now=lambda: q.clock[0],
    )
    current = QuoteFileReadHandler(
        access,
        files,
        document_store,
        slot,
        fingerprints,
        maximum_bytes=2_000_000,
        history=False,
    )
    history = QuoteFileReadHandler(
        access,
        files,
        document_store,
        slot,
        fingerprints,
        maximum_bytes=2_000_000,
        history=True,
    )
    registry = ToolRegistry()
    register_quote_file_tools(
        registry,
        generate=generate,
        read=current,
        history=history,
        recovery=NotYetRecovery(),
    )
    checks = {
        "tenant": QuoteFileTenantCheck(slot),
        "permission": QuoteFilePermissionCheck(q.actors, access, slot),
        "approval": QuoteFileApprovalCheck(access, slot),
        "idempotency": IdempotencyCheck(),
        "rate_limit": QuoteFileRateLimitCheck(ControlledRate(), slot),
    }
    gateway = ToolGateway(
        registry,
        checks,
        ledger_factory,
        lease_duration=timedelta(seconds=30),
        lease_owner="quote-files-test",
        now=lambda: datetime.now(UTC),
        id_factory=new_id,
    )
    app = QuoteFilesApplication(
        gateway, access, files, slot, ledger, fingerprints, generate_tool_version="v1"
    )
    return SimpleNamespace(
        approval=a,
        tenant_id=q.tenant,
        quote_id=a.quote.content.quote_id,
        sales_id=a.quote.content.owner_id,
        manager_id="emp_manager",
        access=access,
        files=files,
        store=store,
        document_store=document_store,
        objects=objects,
        renderer=renderer,
        app=app,
        gateway=gateway,
        slot=slot,
        ledger=ledger,
        ledger_factory=ledger_factory,
        sessions=sessions,
        registry=registry,
        generate=generate,
        fingerprints=fingerprints,
        real_receipt=approved.receipt,
    )


async def test_cross_actor_duplicate_keeps_one_file(file_gateway_case):
    c = file_gateway_case
    a = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    b = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
    assert a == b and c.renderer.calls == c.objects.puts == 1
    assert c.objects.gets == 0 and c.slot.take() is None
    current, data = await c.app.download(
        c.tenant_id, c.quote_id, a.file_id, actor_id=c.sales_id
    )
    assert current == a and data.startswith(b"%PDF-")
    historical, history_bytes = await c.app.read_history(
        c.tenant_id, c.quote_id, a.file_id, actor_id=c.manager_id
    )
    assert historical == a and history_bytes == data
    async with c.sessions() as db:
        rows = list(
            (
                await db.scalars(
                    select(ToolCallRow).where(ToolCallRow.tenant_id == c.tenant_id)
                )
            ).all()
        )
    assert {r.user_id for r in rows} == {c.sales_id, c.manager_id}
    assert all(r.run_id is r.campaign_id is r.message_attempt_id is None for r in rows)


@pytest.mark.parametrize(
    "employee_id", ["员工编号", "employee internal", "emp_token_1"]
)
async def test_real_ledger_unrepresentable_employee_fails_without_fake_call_id(
    file_gateway_case, employee_id
):
    c = file_gateway_case
    async with c.sessions.begin() as db:
        db.add(
            EmployeeRow(
                tenant_id=c.tenant_id,
                employee_id=employee_id,
                role="boss",
                is_active=True,
                name="Controlled",
                created_at=c.approval.quotation.clock[0],
            )
        )
    with pytest.raises(Exception) as error:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=employee_id)
    assert error.value.detail.code == "invalid_input"
    assert (
        error.value.detail.tool_call_id
        is error.value.detail.original_generation_call_id
        is None
    )
    assert c.objects.puts == c.renderer.calls == 0 and c.slot.take() is None


@pytest.mark.parametrize("length", [33, 40])
async def test_long_actor_dto_facts_reach_real_ledger_rejection(
    file_gateway_case, length
):
    """员工表本身32上限；此处只证明受控事实→真实Gateway，不伪称可持久。"""
    c = file_gateway_case

    class ControlledAccess:
        async def authorize(self, tenant, quote, *, actor_id):
            assert actor_id == "a" * length
            return await c.access.authorize(tenant, quote, actor_id=c.sales_id)

    app = QuoteFilesApplication(
        c.gateway,
        ControlledAccess(),
        c.files,
        c.slot,
        c.ledger,
        c.fingerprints,
        generate_tool_version="v1",
    )
    with pytest.raises(Exception) as error:
        await app.generate(c.tenant_id, c.quote_id, actor_id="a" * length)
    assert error.value.detail.code == "invalid_input"
    assert (
        error.value.detail.tool_call_id
        is error.value.detail.original_generation_call_id
        is None
    )
    assert c.renderer.calls == c.objects.puts == 0 and c.slot.take() is None


@pytest.mark.parametrize("history", [False, True])
async def test_revocation_after_bounded_read_discards_bytes(file_gateway_case, history):
    c = file_gateway_case
    file = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)

    async def revoke():
        async with c.sessions.begin() as db:
            await db.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == c.tenant_id,
                    EmployeeRow.employee_id == c.sales_id,
                )
                .values(is_active=False)
            )

    c.objects.after_read = revoke
    read = c.app.read_history if history else c.app.download
    with pytest.raises(Exception) as error:
        await read(c.tenant_id, c.quote_id, file.file_id, actor_id=c.sales_id)
    assert (
        error.value.detail.code == "permission_denied"
        and error.value.detail.tool_call_id is not None
    )
    assert c.slot.take() is None and c.objects.puts == 1
    # owner失活也关闭经理scope；先恢复当前scope再验证原文件仍在。
    async with c.sessions.begin() as db:
        await db.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == c.tenant_id,
            EmployeeRow.employee_id == c.sales_id).values(is_active=True))
    assert (
        await c.files.get_file(
            c.tenant_id, c.quote_id, file.file_id, actor_id=c.manager_id
        )
        == file
    )


async def test_formal_expiry_does_not_fallback_to_history(file_gateway_case):
    c = file_gateway_case
    file = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    c.approval.quotation.clock[0] += timedelta(days=40)
    with pytest.raises(Exception) as error:
        await c.app.download(c.tenant_id, c.quote_id, file.file_id, actor_id=c.sales_id)
    assert error.value.detail.code == "quote_expired" and c.objects.gets == 0
    assert (
        await c.app.read_history(
            c.tenant_id, c.quote_id, file.file_id, actor_id=c.sales_id
        )
    )[0] == file


async def test_duplicate_rechecks_persistent_tool_version(file_gateway_case):
    c = file_gateway_case
    await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    async with c.sessions.begin() as db:
        await db.execute(
            update(ToolCallRow)
            .where(
                ToolCallRow.tenant_id == c.tenant_id,
                ToolCallRow.tool_id == "quotation.file.generate",
            )
            .values(tool_version="v0")
        )
    with pytest.raises(Exception) as error:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
    assert (
        error.value.detail.code == "idempotency_conflict"
        and error.value.detail.original_generation_call_id is None
    )
    assert c.objects.puts == c.renderer.calls == 1


async def test_invalid_input_and_cross_tenant_have_zero_object_io(file_gateway_case):
    c = file_gateway_case
    for tenant, quote in ((new_id("tn"), c.quote_id), (c.tenant_id, "quo_bad")):
        with pytest.raises(QuoteFileApplicationError):
            await c.app.generate(tenant, quote, actor_id=c.sales_id)
    assert c.objects.puts == c.renderer.calls == c.objects.gets == 0
