"""服务门面当前身份与单一创建session规则；存储仅为受控端口。"""
import importlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from domains.quotations import schemas as q, service as public
from tests.unit.test_quote_context_contracts import employee
from tests.unit.test_need_units import NOW, TENANT, ACTOR


def service_case(*, fact=None):
    assert importlib.util.find_spec("domains.quotations.service_impl"), "缺少报价服务实现"
    actors=AsyncMock()
    actors.read_current.return_value=fact if fact is not None else employee()
    quotes=AsyncMock()
    quotes.get.return_value=None
    quotes.list_versions.return_value=()
    quotes.get_by_operation.return_value=None
    @asynccontextmanager
    async def factory(tenant):
        assert tenant==TENANT
        uow=AsyncMock()
        uow.quotes=quotes
        yield uow
    impl=importlib.import_module("domains.quotations.service_impl").QuotationServiceImpl
    svc=impl(factory,actors,public.StrictQuotePreparationPolicy(),AsyncMock(),now=lambda:NOW)
    return svc,actors,quotes


@pytest.mark.parametrize("role",["sales","manager","viewer"])
async def test_internal_read_does_not_expand_crm_roles(role):
    svc,actors,quotes=service_case(fact=employee(role))
    from domains.quotations.errors import QuotationPermissionError
    with pytest.raises(QuotationPermissionError):
        await svc.get(TENANT,"absent",actor=q.QuotationActor(employee_id=ACTOR,role=role))
    quotes.get.assert_not_awaited()


async def test_missing_actor_is_denied_before_existence_is_disclosed():
    svc,actors,quotes=service_case()
    actors.read_current.return_value=None
    from domains.quotations.errors import QuotationPermissionError
    with pytest.raises(QuotationPermissionError):
        await svc.get(TENANT,"absent",actor=q.QuotationActor(employee_id=ACTOR,role="boss"))
    quotes.get.assert_not_awaited()


async def test_authorized_missing_quote_is_fixed_error():
    svc,_,_=service_case()
    from domains.quotations.errors import QuotationError
    with pytest.raises(QuotationError) as e:
        await svc.get(TENANT,"absent",actor=q.QuotationActor(employee_id=ACTOR,role="boss"))
    assert e.value.code=="quote_not_found"


async def test_session_requires_exact_preflight_and_convenience_reuses_gate():
    from tests.unit.test_quotation_contracts import basis_case
    from domains.quotations.errors import QuotationError
    svc,_,repo=service_case()
    assert hasattr(svc,"open_creation"), "缺少同事务创建session"
    i,b,c,now=basis_case()
    repo.get_issuer.return_value=c.issuer
    actor=q.QuotationActor(employee_id=ACTOR,role="boss")
    async with svc.open_creation(TENANT,c.opportunity_id,c,actor=actor) as session:
        with pytest.raises(QuotationError) as e:
            await session.create_from_basis(i,b,operation_id=b.operation_id)
        assert e.value.code=="invalid_input"
        assert await session.preflight(i,operation_id=None) is None
        with pytest.raises(QuotationError):
            await session.create_from_basis(i.model_copy(update={"quote_fx_ref":"other"}),b,operation_id=b.operation_id)
    repo.add.assert_not_awaited()
    created=await svc.create_from_basis(TENANT,i,b,c,operation_id=b.operation_id,actor=actor)
    assert created.state==q.QuoteState.DRAFT
    assert created.content.version==1
    assert created.content.prepared_by==ACTOR


async def test_session_rechecks_actor_after_lock_wait():
    from tests.unit.test_quotation_contracts import basis_case
    from domains.quotations.errors import QuotationPermissionError
    svc,actors,repo=service_case()
    assert hasattr(svc,"open_creation"), "缺少同事务创建session"
    i,b,c,now=basis_case()
    async def lock(*args):
        actors.read_current.return_value=employee(is_active=False)
    repo.lock_opportunity.side_effect=lock
    with pytest.raises(QuotationPermissionError):
        async with svc.open_creation(TENANT,c.opportunity_id,c,actor=q.QuotationActor(employee_id=ACTOR,role="boss")):
            pytest.fail("锁等待后失活必须拒绝")


@pytest.mark.parametrize("state,replaces,error",[("draft",False,"active_quote_exists"),
    ("expired",False,"revision_conflict"),("accepted",True,"revision_conflict"),("rejected",True,"revision_conflict")])
async def test_revision_preflight_rejects_illegal_replacement(state,replaces,error):
    from tests.unit.test_quotation_contracts import basis_case
    from domains.quotations.errors import QuotationError
    svc,_,repo=service_case()
    assert hasattr(svc,"open_creation"), "缺少同事务创建session"
    i,b,c,now=basis_case()
    old=public.build_quote_content("old_quote",1,i,b,c,created_at=now,replaced_quote_version=None)
    repo.list_versions.return_value=(q.QuoteDetailView(content=old,state=q.QuoteState(state)),)
    repo.get_issuer.return_value=c.issuer
    if replaces:
        i=i.model_copy(update={"replaces_quote_id":"old_quote","expected_quote_version":1})
    with pytest.raises(QuotationError) as e:
        async with svc.open_creation(TENANT,c.opportunity_id,c,actor=q.QuotationActor(employee_id=ACTOR,role="boss")) as session:
            await session.preflight(i,operation_id=None)
    assert e.value.code==error
