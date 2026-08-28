"""真实T4创建→T5审批receipt→T6 Store，文件actor不冒充审批人。"""

# ruff: noqa: PLC0414 -- pytest真实fixture显式重导出

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import update

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
    c = approved_file_case
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


async def test_file_actor_is_not_send_decider(file_access_case):
    c = file_access_case
    assert c.sales_id != c.receipt.quote_send_decider
    snapshot = await c.access.authorize(c.tenant, c.quote_id, actor_id=c.sales_id)
    assert snapshot.approval_run_id == c.receipt.approval_run_id
    assert snapshot.customer_content_hash == customer_quote_hash(snapshot.customer)
    assert c.blobs.gets == 0


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
