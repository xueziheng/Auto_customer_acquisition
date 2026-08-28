"""安全集合读取必须区分真实空机会、缺对象与存储故障。"""

import pytest
from sqlalchemy.exc import OperationalError

from domains.costing.permissions import CostingActor, CostingScope
from shared.errors import PermissionDenied
from shared.schemas.identifiers import OpportunityId, TenantId, new_id
from tests.integration.test_costing_quote_evidence import BOSS, expense, setup


async def test_price_list_existing_opportunity_without_any_cost_sheet_is_empty(
    integration_engine,
):
    service, tenant, opportunity, *_ = await setup(integration_engine)
    assert hasattr(service, "list_price_evidence"), "缺少安全价格依据集合读取"
    assert await service.list_price_evidence(tenant, opportunity, actor=BOSS) == ()


@pytest.mark.parametrize("cross_tenant", [False, True])
async def test_price_list_missing_and_cross_tenant_opportunity_are_fixed_not_found(
    integration_engine, cross_tenant
):
    from domains.costing import service as public

    service, tenant, opportunity, _, actors, *_ = await setup(integration_engine)
    assert hasattr(service, "list_price_evidence"), "缺少安全价格依据集合读取"
    if cross_tenant:
        tenant = TenantId(new_id("tn"))
        actors.tenant = tenant
    else:
        opportunity = OpportunityId("opp_missing")
    with pytest.raises(public.CostingQuoteNotFoundError) as error:
        await service.list_price_evidence(tenant, opportunity, actor=BOSS)
    assert error.value.code == "record_not_found"
    assert str(error.value) == "成本报价记录不存在"


async def test_price_list_returns_real_stable_evidence_and_no_other_opportunity(
    integration_engine,
):
    service, tenant, opportunity, artifact, *_ = await setup(integration_engine)
    assert hasattr(service, "list_price_evidence"), "缺少安全价格依据集合读取"
    first = await service.confirm_price(
        tenant, expense(opportunity, artifact), actor=BOSS, idempotency_key="first-list"
    )
    second = await service.confirm_price(
        tenant,
        expense(opportunity, artifact, amount="13.00"),
        actor=BOSS,
        idempotency_key="second-list",
    )
    result = await service.list_price_evidence(tenant, opportunity, actor=BOSS)
    assert result == tuple(
        sorted((first, second), key=lambda v: (v.confirmed_at, v.evidence_id))
    )


async def test_price_list_denial_does_not_open_any_fact_uow(integration_engine):
    service, tenant, opportunity, _, actors, *_ = await setup(integration_engine)
    assert hasattr(service, "list_price_evidence"), "缺少安全价格依据集合读取"
    actor = CostingActor(BOSS.actor_id, "sales", CostingScope.TENANT)
    actors.actors[BOSS.actor_id] = actor

    def forbidden(_):
        pytest.fail("拒权后不得读取存在性或价格事实")

    service._factory = forbidden
    with pytest.raises(PermissionDenied):
        await service.list_price_evidence(tenant, opportunity, actor=actor)


async def test_price_list_storage_failure_is_not_missing_or_empty(
    integration_engine, monkeypatch
):
    from infra.db.repositories import costing_quote

    service, tenant, opportunity, *_ = await setup(integration_engine)
    assert hasattr(service, "list_price_evidence"), "缺少安全价格依据集合读取"
    failure = OperationalError(
        "controlled statement", {}, Exception("controlled unavailable")
    )

    async def broken(*_):
        raise failure

    monkeypatch.setattr(
        costing_quote.CostingOpportunityReferenceReaderImpl, "exists", broken
    )
    with pytest.raises(OperationalError) as error:
        await service.list_price_evidence(tenant, opportunity, actor=BOSS)
    assert error.value is failure
