"""审批持久请求真实PG验收；员工/报价/run完整闭环另由同模块逐片扩展。"""

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.approvals.errors import QuoteContractError
from domains.approvals.service_impl import ApprovalServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.tables import ApprovalPackageRow
from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.integration.test_quotation_creation_recovery import (
    context_case as context_case,  # noqa: PLC0414 -- pytest显式重导出真实数据库夹具
)
from tests.integration.test_quotation_creation_recovery import (
    freeze_case as freeze_case,  # noqa: PLC0414 -- pytest显式重导出真实数据库夹具
)
from tests.integration.test_quotation_creation_recovery import (
    prepared_quote as prepared_quote,  # noqa: PLC0414 -- pytest显式重导出真实数据库夹具
)
from tests.integration.test_quotation_creation_recovery import (
    quotation_case as quotation_case,  # noqa: PLC0414 -- pytest显式重导出真实数据库夹具
)
from tests.integration.test_quotation_creation_recovery import (
    recovery_case as recovery_case,  # noqa: PLC0414 -- pytest显式重导出真实数据库夹具
)
from tests.integration.test_quotation_creation_recovery import (
    unit_db_case as unit_db_case,  # noqa: PLC0414 -- pytest显式重导出真实数据库夹具
)
from tests.unit.test_approval_service import QuoteAccessCase, submit_quote
from tests.unit.test_quotation_contracts import basis_case


@pytest_asyncio.fixture
async def approval_case(recovery_case):
    from decimal import Decimal
    from types import SimpleNamespace

    from domains.costing.approval_policy import CostingApprovalPolicyReaderImpl
    from domains.quotations.service import StrictQuotePreparationPolicy
    from infra.db.tables import EmployeeRow
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.schemas.identifiers import EmployeeId, new_id
    from shared.schemas.money import Money
    from tests.integration.test_quotations import NoSend
    from workflows.engine.runner import StepDefinition, WorkflowDefinition
    from workflows.quote_approval.approvals import QuotationApprovalAccess
    from workflows.quote_approval.policy_reader import CostingQuoteApprovalPolicyReader
    from workflows.quote_approval.run_reader import WorkflowQuoteRunReader
    r = recovery_case
    r.command = r.command.model_copy(update={"unit_price":Money(
        amount=r.command.unit_price.amount/Decimal(10),currency=r.command.unit_price.currency)})
    quote = await r.create()
    c = r.quotation
    engine_holder = [None]
    provider = r.freeze.provider
    policies = CostingQuoteApprovalPolicyReader(CostingApprovalPolicyReaderImpl(
        r.freeze.factory,now=lambda:c.clock[0]))
    c.service = type(c.service)(c.factory,c.actors,StrictQuotePreparationPolicy(),NoSend(),
        context_provider=provider,approval_policy_reader=policies,
        workflow_run_reader=WorkflowQuoteRunReader(lambda:engine_holder[0]),now=lambda:c.clock[0])
    approvals = ApprovalServiceImpl(lambda tenant:SqlAlchemyApprovalUnitOfWork(
        c.context.unit.sessions,tenant,now=lambda:c.clock[0]),
        quote_access=QuotationApprovalAccess(c.service),now=lambda:c.clock[0])
    class Complete:
        async def execute(self,run):
            return "complete",None,{}
    engine = PostgresWorkflowEngine(c.context.unit.sessions,{"controlled":Complete()},now=lambda:c.clock[0])
    engine.register(WorkflowDefinition("quote_approval",1,(StepDefinition("assemble","controlled"),),{}))
    engine_holder[0] = engine
    run = await engine.start(c.tenant,"quote_approval",quote.content.quote_id,
        {"quote_id":quote.content.quote_id,"quote_version":quote.content.version,
         "content_hash":quote.content.content_hash,"prepared_by":quote.content.prepared_by,
         "initiated_by":c.actor.employee_id},"quote-approval-test")
    from domains.quotations.schemas import QuoteWorkflowExecutor
    decider = EmployeeId(new_id("emp"))
    async with c.context.unit.sessions.begin() as session:
        session.add(EmployeeRow(tenant_id=c.tenant,employee_id=decider,name="Independent boss",
            role="boss",is_active=True,created_at=c.clock[0]))
    return SimpleNamespace(creation=r,quotation=c,quote=quote,approvals=approvals,engine=engine,
        provider=provider,policies=policies,decider=decider,
        executor=QuoteWorkflowExecutor(workflow_type="quote_approval",run_id=run,quote_id=quote.content.quote_id))


async def bind_real_approvals(c):
    from domains.approvals.service import ApprovalType, BlastRadius
    from domains.quotations.schemas import QuoteApprovalSubmission
    from domains.quotations.service import quote_change_set_ref
    from workflows.quote_approval.approvals import read_quote_facts
    qcase = c.quotation
    quote = c.quote.content
    async with (
        c.provider.open(qcase.tenant, quote.opportunity_id, qcase.actor.employee_id, prepared_by=quote.prepared_by) as context,
        qcase.service.open_approval(qcase.tenant, quote.quote_id, executor=c.executor) as session,
    ):
        snapshot = await session.prepare_submission(context,actor=qcase.actor)
        ids = []
        for payload in snapshot.payloads:
            ids.append(await c.approvals.submit(qcase.tenant,ApprovalType(payload.approval_type),
                "正式报价审批",payload.model_dump(mode="json"),"报价版本独立审批",
                BlastRadius([quote.quote_id],"允许本报价版本","关闭本轮",False),
                proposed_by_run=c.executor.run_id,proposed_by_employee=quote.prepared_by,
                owner_employee=quote.owner_id,evidence_refs=[],
                change_set_ref=quote_change_set_ref(quote.quote_id,quote.content_hash,payload.approval_type),
                expires_at_limit=snapshot.expires_at_limit))
        facts = await read_quote_facts(c.approvals,qcase.tenant,tuple(ids))
        submission = QuoteApprovalSubmission(tenant_id=qcase.tenant,quote_id=quote.quote_id,
            quote_version=quote.version,content_hash=quote.content_hash,policy_id=quote.basis.policy_id,
            policy_hash=quote.basis.policy.content_hash,required_types=snapshot.required_types,facts=facts)
        await session.bind(submission,context,actor=qcase.actor)
    return tuple(ids)


async def test_real_quote_all_approvals_event_and_receipt_commit_once(approval_case):
    from domains.quotations.schemas import QuoteState
    from workflows.quote_approval.approvals import read_quote_facts
    c = approval_case
    ids = await bind_real_approvals(c)
    for approval_id in ids:
        await c.approvals.decide(c.quotation.tenant,approval_id,approved=True,decided_by=c.decider)
    facts = await read_quote_facts(c.approvals,c.quotation.tenant,ids)
    async with (
        c.provider.open_for_approval(c.quotation.tenant, c.quote.content.opportunity_id, c.decider, prepared_by=c.quote.content.prepared_by, decider_ids=(c.decider,)) as context,
        c.quotation.service.open_approval(c.quotation.tenant, c.quote.content.quote_id, executor=c.executor) as session,
    ):
        result = await session.apply(facts,context)
    assert result.outcome == "approved" and result.quote.state == QuoteState.APPROVED
    receipt = await c.quotation.service.get_approval_application(c.quotation.tenant,
        c.quote.content.quote_id,executor=c.executor)
    assert receipt == result.receipt
    async with c.quotation.context.unit.sessions() as session:
        for table,extra in (("quotation_approval_receipts",""),("outbox_events"," AND event_type='QuoteApproved'"),
                            ("quotation_state_events"," AND to_state='approved'")):
            assert await session.scalar(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"+extra),
                {"tenant":c.quotation.tenant}) == 1


async def test_handler_can_commit_receipt_while_own_run_is_locked(approval_case):
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from workflows.engine.runner import StepDefinition, StepStatus, WorkflowDefinition
    from workflows.quote_approval.approvals import read_quote_facts
    c = approval_case
    ids = await bind_real_approvals(c)
    for approval_id in ids:
        await c.approvals.decide(c.quotation.tenant,approval_id,approved=True,decided_by=c.decider)
    failures = []
    class Apply:
        async def execute(self,run):
            facts = await read_quote_facts(c.approvals,c.quotation.tenant,ids)
            try:
                async with (
                    c.provider.open_for_approval(c.quotation.tenant, c.quote.content.opportunity_id, c.decider, prepared_by=c.quote.content.prepared_by, decider_ids=(c.decider,)) as context,
                    c.quotation.service.open_approval(c.quotation.tenant, c.quote.content.quote_id, executor=c.executor) as session,
                ):
                    await session.apply(facts,context)
            except Exception as error:
                failures.append(getattr(error,"code",type(error).__name__))
                raise
            return "complete",None,{}
    engine = PostgresWorkflowEngine(c.quotation.context.unit.sessions,{"controlled":Apply()},
        now=lambda:c.quotation.clock[0])
    engine.register(WorkflowDefinition("quote_approval",1,(StepDefinition("assemble","controlled"),),{}))
    async with asyncio.timeout(5):
        await engine.poll_due(c.quotation.tenant,limit=1)
    run = await engine.get_run(c.quotation.tenant,c.executor.run_id)
    assert run.status == StepStatus.COMPLETED, failures


async def test_event_handler_can_commit_receipt_with_actual_run_fk(approval_case):
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from workflows.engine.runner import StepDefinition, StepStatus, WorkflowDefinition
    from workflows.quote_approval.approvals import read_quote_facts
    c = approval_case
    await c.engine.cancel(c.quotation.tenant,c.executor.run_id,"controlled reassembly")
    failures = []
    class Apply:
        async def execute(self,run):
            facts = await read_quote_facts(c.approvals,c.quotation.tenant,ids)
            try:
                async with (
                    c.provider.open_for_approval(c.quotation.tenant, c.quote.content.opportunity_id, c.decider, prepared_by=c.quote.content.prepared_by, decider_ids=(c.decider,)) as context,
                    c.quotation.service.open_approval(c.quotation.tenant, c.quote.content.quote_id, executor=c.executor) as session,
                ):
                    await session.apply(facts,context)
            except Exception as error:
                failures.append(getattr(error,"code",type(error).__name__))
                raise
            return "complete",None,{}
    engine = PostgresWorkflowEngine(c.quotation.context.unit.sessions,{"controlled":Apply()},
        now=lambda:c.quotation.clock[0])
    engine.register(WorkflowDefinition("quote_approval",1,(StepDefinition("assemble","controlled",
        wait_event_type="ApprovalDecided"),),{}))
    old = await c.engine.get_run(c.quotation.tenant,c.executor.run_id)
    run_id = await engine.start(c.quotation.tenant,"quote_approval",old.subject_ref,old.context,"event-receipt")
    c.executor = c.executor.model_copy(update={"run_id":run_id})
    ids = await bind_real_approvals(c)
    for approval_id in ids:
        await c.approvals.decide(c.quotation.tenant,approval_id,approved=True,decided_by=c.decider)
    async with asyncio.timeout(5):
        await engine.deliver_event(c.quotation.tenant,run_id,"ApprovalDecided",{"approval_id":ids[-1]})
    run = await engine.get_run(c.quotation.tenant,run_id)
    assert run.status == StepStatus.COMPLETED, failures




@pytest.mark.parametrize("fault", ["transition","publish","receipt"])
async def test_quote_success_effects_roll_back_together(approval_case,monkeypatch,fault):
    from domains.quotations.errors import QuoteApprovalUnavailableError
    from infra.db.outbox import PostgresEventBus
    from infra.db.repositories.quotations import QuotationVersionRepositoryImpl
    from workflows.quote_approval.approvals import read_quote_facts
    c = approval_case
    ids = await bind_real_approvals(c)
    for approval_id in ids:
        await c.approvals.decide(c.quotation.tenant,approval_id,approved=True,decided_by=c.decider)
    target,name = (PostgresEventBus,"publish") if fault=="publish" else (
        QuotationVersionRepositoryImpl,"transition" if fault=="transition" else "add_approval_receipt")
    original = getattr(target,name)
    async def fail_after_write(self,*args,**kwargs):
        await original(self,*args,**kwargs)
        raise QuoteApprovalUnavailableError("storage_unknown")
    monkeypatch.setattr(target,name,fail_after_write)
    facts = await read_quote_facts(c.approvals,c.quotation.tenant,ids)
    with pytest.raises(QuoteApprovalUnavailableError):
        async with (
            c.provider.open_for_approval(c.quotation.tenant, c.quote.content.opportunity_id, c.decider, prepared_by=c.quote.content.prepared_by, decider_ids=(c.decider,)) as context,
            c.quotation.service.open_approval(c.quotation.tenant, c.quote.content.quote_id, executor=c.executor) as session,
        ):
            await session.apply(facts,context)
    async with c.quotation.context.unit.sessions() as session:
        assert await session.scalar(text("SELECT state FROM quotations WHERE tenant_id=:tenant AND quote_id=:quote"),
            {"tenant":c.quotation.tenant,"quote":c.quote.content.quote_id}) == "pending_approval"
        for table,extra in (("quotation_approval_receipts",""),("outbox_events"," AND event_type='QuoteApproved'"),
                            ("quotation_state_events"," AND to_state='approved'")):
            assert await session.scalar(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"+extra),
                {"tenant":c.quotation.tenant}) == 0


async def test_actual_run_binding_is_checked_by_every_quotation_entry(approval_case):
    import json

    from domains.quotations.errors import QuoteApprovalError
    c = approval_case
    old = await c.engine.get_run(c.quotation.tenant,c.executor.run_id)
    changes = [
        {"workflow_type":"other"},{"workflow_version":2},{"subject_ref":"other"},
        {"context":{**old.context,"quote_version":2}},
        {"context":{**old.context,"quote_version":True}},
        {"context":{**old.context,"content_hash":"f"*64}},
        {"context":{k:v for k,v in old.context.items() if k!="content_hash"}},
        {"context":{k:v for k,v in old.context.items() if k!="quote_version"}},
    ]
    for changed in changes:
        values = {"workflow_type":old.workflow_type,"workflow_version":old.workflow_version,
                  "subject_ref":old.subject_ref,"context":old.context,**changed}
        async with c.quotation.context.unit.sessions.begin() as session:
            await session.execute(text("UPDATE workflow_runs SET workflow_type=:kind,workflow_version=:version,"
                "subject_ref=:subject,context=CAST(:context AS jsonb) WHERE tenant_id=:tenant AND run_id=:run"),
                {"tenant":c.quotation.tenant,"run":c.executor.run_id,"kind":values["workflow_type"],
                 "version":values["workflow_version"],"subject":values["subject_ref"],"context":json.dumps(values["context"])})
        for entry in ("target","session","receipt"):
            with pytest.raises(QuoteApprovalError) as error:
                if entry == "target":
                    await c.quotation.service.approval_target(c.quotation.tenant,c.quote.content.quote_id,executor=c.executor)
                elif entry == "receipt":
                    await c.quotation.service.get_approval_application(c.quotation.tenant,c.quote.content.quote_id,executor=c.executor)
                else:
                    async with c.quotation.service.open_approval(c.quotation.tenant,c.quote.content.quote_id,executor=c.executor):
                        pytest.fail("损坏真实run不能进入session")
            assert error.value.code == "workflow_binding_invalid"




@pytest.mark.parametrize("entry",["poll","event"])
async def test_engine_execution_lease_blocks_writes_delete_and_cancel(unit_engine,entry):
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from workflows.engine.runner import StepDefinition, StepStatus, WorkflowDefinition
    sessions = async_sessionmaker(unit_engine,expire_on_commit=False)
    entered,release = asyncio.Event(),asyncio.Event()
    calls = []
    class Paused:
        async def execute(self,run):
            calls.append(run.run_id)
            entered.set()
            await release.wait()
            return "complete",None,{}
    engine = PostgresWorkflowEngine(sessions,{"pause":Paused()},now=lambda:basis_case()[3])
    engine.register(WorkflowDefinition("lock_contract",1,(StepDefinition("pause","pause",
        wait_event_type="ApprovalDecided" if entry=="event" else None),),{}))
    tenant = "tenant-run-lock"
    run_id = await engine.start(tenant,"lock_contract","subject",{},"key")
    running = asyncio.create_task(engine.poll_due(tenant,1) if entry=="poll" else
        engine.deliver_event(tenant,run_id,"ApprovalDecided",{"approval_id":"controlled"}))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            for statement in ("UPDATE workflow_runs SET retry_count=retry_count+1",
                              "UPDATE workflow_runs SET run_id=run_id||'x'","DELETE FROM workflow_runs"):
                async with sessions() as session:
                    await session.execute(text("SET LOCAL lock_timeout='150ms'"))
                    with pytest.raises(DBAPIError) as error:
                        await session.execute(text(statement+" WHERE tenant_id=:tenant AND run_id=:run"),
                            {"tenant":tenant,"run":run_id})
                    assert getattr(error.value.orig,"sqlstate",None) == "55P03"
                    await session.rollback()
            assert not running.done()
            # 另一次poll跳过已锁step；event/cancel会等待，不能重复执行或复活终态。
            assert await engine.poll_due(tenant,1) == 0
            cancelling = asyncio.create_task(engine.cancel(tenant,run_id,"controlled concurrent cancel"))
            async with sessions() as observer:
                while True:
                    await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    blocked = await observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE "
                        "datname=current_database() AND wait_event_type='Lock' AND pid<>pg_backend_pid()"))
                    if blocked:
                        break
                    assert not cancelling.done()
                    await asyncio.sleep(0)
            release.set()
            await running
            await cancelling
    finally:
        release.set()
        await running
    assert calls == [run_id]
    assert (await engine.get_run(tenant,run_id)).status == StepStatus.COMPLETED




async def test_all_deciders_and_policy_remain_locked_until_quote_commit(approval_case,monkeypatch):
    from decimal import Decimal

    from infra.db.quotation_uow import SqlAlchemyQuotationUow
    from shared.schemas.identifiers import EmployeeId
    from tests.integration.test_costing_quote_evidence import policy
    from workflows.quote_approval.approvals import read_quote_facts
    c = approval_case
    await c.quotation.context.update_owner_manager()
    ids = await bind_real_approvals(c)
    await c.approvals.decide(c.quotation.tenant,ids[0],approved=True,decided_by=c.decider)
    manager = EmployeeId("emp_manager")
    await c.approvals.decide(c.quotation.tenant,ids[1],approved=True,decided_by=manager)
    entered,release = asyncio.Event(),asyncio.Event()
    original = SqlAlchemyQuotationUow.commit
    async def paused(self):
        entered.set()
        await release.wait()
        await original(self)
    monkeypatch.setattr(SqlAlchemyQuotationUow,"commit",paused)
    async def apply():
        facts = await read_quote_facts(c.approvals,c.quotation.tenant,ids)
        async with (
            c.provider.open_for_approval(c.quotation.tenant, c.quote.content.opportunity_id, c.decider, prepared_by=c.quote.content.prepared_by, decider_ids=(manager, c.decider)) as context,
            c.quotation.service.open_approval(c.quotation.tenant, c.quote.content.quote_id, executor=c.executor) as session,
        ):
            return await session.apply(facts,context)
    applying = asyncio.create_task(apply())
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            deactivating = asyncio.create_task(c.quotation.context.update_employee(manager,is_active=False))
            reassigning = asyncio.create_task(c.quotation.context.update_employee(EmployeeId("emp_owner"),manager_id=None))
            f = c.creation.freeze
            confirming = asyncio.create_task(f.t2.confirm_policy(c.quotation.tenant,
                policy(f.evidence.source_ref,minimum_margin_rate=Decimal("0.15"),effective_from=c.quotation.clock[0]),
                actor=f.actor,idempotency_key="approval-policy-race"))
            async with c.quotation.context.unit.sessions() as observer:
                while True:
                    assert not any(task.done() for task in (deactivating,reassigning,confirming))
                    await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    count = await observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE "
                        "datname=current_database() AND wait_event_type='Lock' AND pid<>pg_backend_pid()"))
                    if count>=3:
                        break
                    await asyncio.sleep(0)
            release.set()
            result = await applying
            await asyncio.gather(deactivating,reassigning,confirming)
    finally:
        release.set()
        await applying
    assert result.receipt is not None


async def test_quote_submit_concurrency_original_limit_and_immutable_decision(
    unit_engine,
):
    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    clock = [basis_case()[3]]
    factory = lambda tenant: SqlAlchemyApprovalUnitOfWork(
        sessions, tenant, now=lambda: clock[0]
    )
    service = ApprovalServiceImpl(
        factory, quote_access=QuoteAccessCase(), now=lambda: clock[0]
    )
    first, second = await asyncio.gather(submit_quote(service), submit_quote(service))
    payload, approval_id = first
    assert first[1] == second[1]
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ApprovalPackageRow)
                .where(ApprovalPackageRow.tenant_id == payload.tenant_id)
            )
            == 1
        )
    before = await service.read_fact(payload.tenant_id, approval_id)
    assert before.expires_at_limit == payload.customer.valid_until
    assert before.expires_at == min(
        before.created_at + timedelta(days=2), before.expires_at_limit
    )
    with pytest.raises(QuoteContractError) as error:
        await submit_quote(
            service, limit=payload.customer.valid_until + timedelta(days=1)
        )
    assert error.value.code == "quote_request_conflict"
    async with sessions.begin() as session:
        await session.execute(
            text("""UPDATE approval_packages SET state='approved',
            decided_by='emp_decider',decided_at=:now WHERE tenant_id=:tenant AND approval_id=:approval"""),
            {"now": clock[0], "tenant": payload.tenant_id, "approval": approval_id},
        )
    await service.mark_applied(payload.tenant_id, approval_id, "quote-test-application")
    clock[0] += timedelta(days=30)
    assert (await submit_quote(service))[1] == approval_id
    after = await service.read_fact(payload.tenant_id, approval_id)
    assert after.request_hash == before.request_hash
    assert after.state.value == "applied"
    for assignment in (
        "decision_note='changed'",
        "request_hash=repeat('b',64)",
        "expires_at_limit=expires_at_limit+interval '1 day'",
    ):
        async with sessions.begin() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        f"UPDATE approval_packages SET {assignment} WHERE tenant_id=:tenant AND approval_id=:approval"
                    ),
                    {"tenant": payload.tenant_id, "approval": approval_id},
                )
    assert migrate(unit_engine, "downgrade", "0044") != 0


async def test_real_engine_reader_uses_mvcc_while_handler_holds_run_lock(unit_engine):
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.schemas.identifiers import TenantId
    from workflows.engine.runner import StepDefinition, WorkflowDefinition

    assert hasattr(PostgresWorkflowEngine, "get_run"), "缺少真实workflow get_run读口"
    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    observed = []

    class Handler:
        async def execute(self, run):
            async with asyncio.timeout(1):
                stored = await engine.get_run(run.tenant_id, run.run_id)
            observed.append(stored.context)
            return ("complete", None, {})

    engine = PostgresWorkflowEngine(sessions, {"test": Handler()})
    engine.register(
        WorkflowDefinition("quote_approval", 1, (StepDefinition("test", "test"),))
    )
    tenant = TenantId("tn_test")
    run_id = await engine.start(
        tenant,
        "quote_approval",
        "quo_01K00000000000000000000000",
        {"quote_version": 1, "content_hash": "a" * 64},
        "quote-reader-test",
    )
    assert await engine.get_run(TenantId("other"), run_id) is None
    assert await engine.poll_due(tenant, 1) == 1
    assert observed == [{"quote_version": 1, "content_hash": "a" * 64}]
    assert (await engine.get_run(tenant, run_id)).status.value == "completed"
