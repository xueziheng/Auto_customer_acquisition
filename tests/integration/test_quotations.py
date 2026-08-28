"""真实报价持久层；当前员工和全部父行真实，外部来源依然受控。"""
import importlib
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from domains.quotations import schemas as q, service as public
from shared.schemas.quote_facts import QuoteEmployeeFact
from tests.integration.test_need_units import NOW, unit_db_case as unit_db_case, unit_engine as unit_engine
from tests.integration.test_quote_context_locks import context_case as context_case
from tests.integration.test_quote_cost_lock import freeze_case as freeze_case


class CurrentActors:
    def __init__(self, context):
        self.context = context

    async def read_current(self, tenant_id, employee_id):
        from infra.db.repositories.employees import EmployeeRepositoryImpl
        async with self.context.unit.sessions() as session:
            employee = await EmployeeRepositoryImpl(session,tenant_id).get(tenant_id,employee_id)
            if employee is None:
                return None
            return QuoteEmployeeFact(tenant_id=tenant_id,employee_id=employee.employee_id,role=employee.role.value,
                is_active=employee.is_active,manager_id=employee.manager_id,team_id=employee.team_id)


class NoSend:
    async def read(self, tenant_id, attempt_id, *, actor_id):
        return None


@dataclass
class QuotationCase:
    context: object
    service: object
    factory: object
    actors: object
    clock: list

    @property
    def tenant(self):
        return self.context.tenant_id

    @property
    def actor(self):
        return q.QuotationActor(employee_id=self.context.actor_id,role="boss")

    async def issuer(self, *, key="issuer", name="Supplier Company"):
        return await self.service.confirm_issuer(self.tenant,q.QuoteIssuerCreate(name=name,address="Test address",
            contact="hello@example.test"),actor=self.actor,idempotency_key=key)


@pytest_asyncio.fixture
async def quotation_case(context_case):
    assert hasattr(public,"QuotationVersionService"), "缺少新报价持久服务端口"
    uow=importlib.import_module("infra.db.quotation_uow").SqlAlchemyQuotationUow
    impl=importlib.import_module("domains.quotations.service_impl").QuotationServiceImpl
    clock=[NOW]
    factory=lambda tid:uow(context_case.unit.sessions,tid,lock_timeout_ms=1500,statement_timeout_ms=3000)
    actors=CurrentActors(context_case)
    service=impl(factory,actors,public.StrictQuotePreparationPolicy(),NoSend(),now=lambda:clock[0])
    return QuotationCase(context_case,service,factory,actors,clock)


async def test_issuer_is_current_boss_confirmed_idempotent_and_versioned(quotation_case):
    c=quotation_case
    first=await c.issuer()
    assert first == await c.issuer()
    assert first.source_ref == first.issuer_id
    for name,p in first.field_provenance.items():
        assert p.source_type.value == "employee_input"
        assert p.source_id == first.issuer_id
        assert p.source_quote == getattr(first,name)
        assert p.confirmed_by == c.actor.employee_id == p.extracted_by
        assert p.confirmed_at == p.extracted_at == NOW
    from domains.quotations.errors import QuotationError, QuotationPermissionError
    with pytest.raises(QuotationError) as e:
        await c.issuer(name="Changed")
    assert e.value.code == "idempotency_conflict"
    second=await c.issuer(key="next",name="Changed")
    assert (await c.service.get_confirmed_issuer(c.tenant)) == second
    assert first.issuer_id != second.issuer_id
    await c.context.update_employee(c.actor.employee_id,role="finance")
    with pytest.raises(QuotationPermissionError):
        await c.service.confirm_issuer(c.tenant,q.QuoteIssuerCreate(name="No",address="No",contact="No"),
            actor=q.QuotationActor(employee_id=c.actor.employee_id,role="finance"),idempotency_key="denied")


@pytest.mark.parametrize("statement", ["UPDATE quotation_issuers SET version=version+1", "DELETE FROM quotation_issuers"])
async def test_issuer_cannot_be_changed_by_sql(quotation_case,statement):
    c=quotation_case
    await c.issuer()
    with pytest.raises(DBAPIError):
        async with c.context.unit.sessions.begin() as session:
            await session.execute(text(statement+" WHERE tenant_id=:tenant"),{"tenant":c.tenant})


async def test_issuer_rechecks_role_after_waiting_for_lock(quotation_case):
    import asyncio
    c=quotation_case
    async with c.factory(c.tenant) as uow:
        await uow.quotes.lock_issuer(c.tenant)
        waiting=asyncio.create_task(c.issuer())
        await c.context.wait_for_blocked_writer(waiting)
        await c.context.update_employee(c.actor.employee_id,is_active=False)
    from domains.quotations.errors import QuotationPermissionError
    with pytest.raises(QuotationPermissionError):
        await waiting


async def test_issuer_concurrent_keys_allocate_contiguous_versions(quotation_case):
    import asyncio
    c=quotation_case
    results=await asyncio.gather(c.issuer(key="one"),c.issuer(key="two"),c.issuer(key="one"))
    assert results[0]==results[2]
    async with c.context.unit.sessions() as session:
        versions=(await session.execute(text("SELECT version FROM quotation_issuers WHERE tenant_id=:tenant ORDER BY version"),
            {"tenant":c.tenant})).scalars().all()
    assert versions==[1,2]


async def test_issuer_tenant_keys_are_isolated(quotation_case):
    from infra.db.tables import EmployeeRow
    from shared.schemas.identifiers import TenantId
    c=quotation_case
    first=await c.issuer()
    other=TenantId("tenant_other_quote")
    async with c.context.unit.sessions.begin() as session:
        session.add(EmployeeRow(tenant_id=other,employee_id="emp_other_quote",name="Other boss",role="boss",
            is_active=True,created_at=NOW))
    second=await c.service.confirm_issuer(other,q.QuoteIssuerCreate(name="Other",address="Other",contact="Other"),
        actor=q.QuotationActor(employee_id="emp_other_quote",role="boss"),idempotency_key="issuer")
    assert second.issuer_id!=first.issuer_id
    assert (await c.service.get_confirmed_issuer(c.tenant)).issuer_id==first.issuer_id
    async with c.factory(other) as uow:
        assert (await uow.quotes.current_issuer_record(other)).version==1
        assert await uow.quotes.get_issuer(other,first.issuer_id) is None


@pytest_asyncio.fixture
async def prepared_quote(quotation_case,freeze_case):
    """真实boss抬头接入context provider，来源reader仍受控。"""
    from infra.db.quote_context import SqlAlchemyQuoteContextProvider
    from workflows.quote_approval.application import QuotePreparationApplication
    c,f=quotation_case,freeze_case
    await c.issuer()
    class IssuerReader:
        async def get_confirmed(self,tenant_id):
            return await c.service.get_confirmed_issuer(tenant_id)
    f.context.provider=SqlAlchemyQuoteContextProvider(c.context.unit.sessions,IssuerReader(),lock_timeout_ms=1500,
        statement_timeout_ms=3000)
    f.application=QuotePreparationApplication(f.provider,f.service,public.StrictQuotePreparationPolicy(),f.application._actors)
    return c,f


@pytest_asyncio.fixture
async def persisted_quote(prepared_quote):
    """真实boss抬头→真实context/freeze→报价仓储；无假FK或假receipt。"""
    from workflows.quote_approval.basis_adapter import to_quote_basis
    from shared.schemas.identifiers import QuoteId,new_id
    c,f=prepared_quote
    i=await f.intent()
    b=await f.freeze(key="stored-quote",intent=i)
    async with f.provider.open(c.tenant,c.context.opportunity_id,c.actor.employee_id,prepared_by=c.actor.employee_id) as context:
        content=public.build_quote_content(QuoteId(new_id("quo")),1,i,to_quote_basis(b),context,created_at=NOW,replaced_quote_version=None)
    detail=q.QuoteDetailView(content=content,state=q.QuoteState.DRAFT)
    async with c.factory(c.tenant) as uow:
        await uow.quotes.lock_opportunity(c.tenant,c.context.opportunity_id)
        await uow.quotes.add(c.tenant,detail)
        await uow.commit()
    return c,f,detail


async def test_quote_real_fk_full_snapshot_and_decimal_roundtrip(persisted_quote):
    c,f,detail=persisted_quote
    loaded=await c.service.get(c.tenant,detail.content.quote_id,actor=c.actor)
    assert loaded==detail
    assert loaded.content.basis.price_evidence[0].amount.amount.as_tuple()==detail.content.basis.price_evidence[0].amount.amount.as_tuple()
    async with c.context.unit.sessions() as session:
        for table in ("quotations","quotation_lines","quotation_evidence_refs","quotation_state_events"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"),{"tenant":c.tenant})==1


@pytest.mark.parametrize("statement",["UPDATE quotations SET version=2", "DELETE FROM quotations",
    "UPDATE quotation_lines SET line_number=2","DELETE FROM quotation_lines",
    "UPDATE quotation_evidence_refs SET kind='confirmed_expense'","DELETE FROM quotation_evidence_refs",
    "UPDATE quotations SET state='expired'"])
async def test_sql_cannot_mutate_quote_or_skip_state_event(persisted_quote,statement):
    c,_,_=persisted_quote
    with pytest.raises(DBAPIError):
        async with c.context.unit.sessions.begin() as session:
            await session.execute(text(statement+" WHERE tenant_id=:tenant"),{"tenant":c.tenant})


async def session_create(c,f,intent,key,*,after_freeze=None):
    """真实lease/机会锁/freezer/报价提交顺序；尚不调用T4应用恢复。"""
    from workflows.quote_approval.application import costing_context
    from workflows.quote_approval.basis_adapter import to_quote_basis,pricing_options_from_intent
    async with f.provider.open(c.tenant,c.context.opportunity_id,c.actor.employee_id,prepared_by=c.actor.employee_id) as context:
        async with c.service.open_creation(c.tenant,c.context.opportunity_id,context,actor=c.actor) as session:
            await session.preflight(intent,operation_id=None)
            frozen=await f.service.freeze(c.tenant,intent.cost_sheet_id,pricing_options_from_intent(intent),costing_context(context),
                idempotency_key=key,intent=intent,actor=f.actor)
            if after_freeze:
                await after_freeze()
            return await session.create_from_basis(intent,to_quote_basis(frozen),operation_id=frozen.operation_id)


async def test_session_real_context_share_does_not_self_wait(prepared_quote):
    import asyncio
    c,f=prepared_quote
    assert hasattr(c.service,"open_creation"), "缺少真实同事务session"
    quote=await asyncio.wait_for(session_create(c,f,await f.intent(),"session-create"),4)
    assert (await c.service.get(c.tenant,quote.content.quote_id,actor=c.actor))==quote
    assert await f.locked_at() is not None


async def test_session_keeps_employee_lease_until_commit(prepared_quote):
    import asyncio
    c,f=prepared_quote
    assert hasattr(c.service,"open_creation"), "缺少真实同事务session"
    writers=[]
    async def during():
        task=asyncio.create_task(c.context.update_employee(c.actor.employee_id,is_active=False))
        writers.append(task)
        await c.context.wait_for_blocked_writer(task)
    quote=await session_create(c,f,await f.intent(),"lease",after_freeze=during)
    await writers[0]
    assert quote.content.version==1
    from domains.quotations.errors import QuotationPermissionError
    with pytest.raises(QuotationPermissionError):
        await c.service.get(c.tenant,quote.content.quote_id,actor=c.actor)


async def test_quote_commit_failure_preserves_frozen_pending_operation(prepared_quote,monkeypatch):
    c,f=prepared_quote
    assert hasattr(c.service,"open_creation"), "缺少真实同事务session"
    repo=importlib.import_module("infra.db.repositories.quotations").QuotationVersionRepositoryImpl
    original=repo.add
    async def fail_after_write(self,*args):
        await original(self,*args)
        raise RuntimeError("controlled before commit")
    monkeypatch.setattr(repo,"add",fail_after_write)
    with pytest.raises(RuntimeError):
        await session_create(c,f,await f.intent(),"rollback")
    operation=await f.service.get_creation(c.tenant,"rollback",actor=f.actor)
    assert operation.state=="frozen"
    assert await c.service.get_by_operation(c.tenant,operation.operation_id,actor=c.actor) is None
    assert await f.locked_at() is not None
