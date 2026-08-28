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


@pytest.mark.parametrize("cross_tenant", [False, True])
async def test_legacy_sheet_read_uses_compatible_typed_missing(
    integration_engine, cross_tenant
):
    from domains.costing import service as public
    from domains.costing.permissions import Phase1CostingAuthorizer
    from domains.costing.service_impl import CostingServiceImpl
    from shared.errors import ValidationError
    from shared.schemas.identifiers import CostSheetId
    from tests.unit.test_costing_service import _quoted_create

    _, tenant, opportunity, _, _, _, factory, _ = await setup(integration_engine)
    service = CostingServiceImpl(factory, Phase1CostingAuthorizer(tenant))
    sheet = CostSheetId(new_id("cost"))
    if cross_tenant:
        sheet = await service.create_sheet(
            tenant, opportunity, _quoted_create(), actor=BOSS
        )
        assert (
            await service.get_sheet(tenant, sheet, actor=BOSS)
        ).cost_sheet_id == sheet
        tenant = TenantId(new_id("tn"))
        service = CostingServiceImpl(factory, Phase1CostingAuthorizer(tenant))
    assert hasattr(public, "CostSheetNotFoundError"), "缺少兼容旧文本的具名缺表错误"
    with pytest.raises(public.CostSheetNotFoundError) as error:
        await service.get_sheet(tenant, sheet, actor=BOSS)
    assert isinstance(error.value, ValidationError)
    assert str(error.value) == "成本表不存在" and error.value.code == "record_not_found"


@pytest.mark.parametrize("action", ["legacy", "calculate", "scope"])
async def test_actual_sheet_reader_keeps_old_http_400_and_new_http_404(
    integration_engine, action
):
    from types import SimpleNamespace

    from domains.costing.freeze_schemas import CostScopeConfirmationCommand
    from domains.costing.permissions import Phase1CostingAuthorizer
    from domains.costing.service_impl import CostingServiceImpl
    from tests.unit.test_costing_router import TENANT, _app
    from tests.unit.test_quotation_contracts import frozen_fixture
    from tests.unit.test_quotation_router import request

    _, _, _, _, _, _, factory, _ = await setup(integration_engine)
    app, _ = _app()
    service = CostingServiceImpl(factory, Phase1CostingAuthorizer(TENANT))
    object.__setattr__(app.state.dependencies, "costing", service)
    object.__setattr__(
        app.state.dependencies,
        "quotation",
        SimpleNamespace(domain=SimpleNamespace(preparation=None)),
    )
    sheet = new_id("cost")
    if action == "legacy":
        response = await request(app, "GET", f"/cost-sheets/{sheet}")
        assert response.status_code == 400
        assert response.json() == {
            "code": "validation_error",
            "message": "请求参数无效",
        }
    else:
        if action == "calculate":
            body = {
                "mode": "manual",
                "unit_price": {"amount": "3.10", "currency": "USD"},
                "rounding": {
                    "unit_places": 2,
                    "total_places": 2,
                    "strategy": "ROUND_HALF_UP",
                },
                "quote_fx_ref": None,
                "algorithm_version": "costing-v1",
            }
            suffix = "calculate"
        else:
            scope = frozen_fixture().scope_confirmation
            body = CostScopeConfirmationCommand(
                coverage_id=scope.coverage_id,
                expected_sheet_hash=scope.sheet_hash,
                expected_coverage_hash=scope.coverage_hash,
                expected_need_facts_hash=scope.need_facts_hash,
                terms=scope.terms,
                valid_until=scope.valid_until,
                evidence_bindings=scope.evidence_bindings,
            ).model_dump(mode="json")
            suffix = "scope-confirmations"
        response = await request(
            app,
            "POST",
            f"/cost-sheets/{sheet}/{suffix}",
            body=body,
            key="missing-sheet",
        )
        assert response.status_code == 404
        assert response.json()["code"] == "record_not_found"


async def test_legacy_sheet_sql_failure_is_not_missing(integration_engine, monkeypatch):
    from domains.costing.permissions import Phase1CostingAuthorizer
    from domains.costing.service_impl import CostingServiceImpl
    from infra.db.repositories.costing import CostSheetRepositoryImpl
    from shared.schemas.identifiers import CostSheetId

    _, tenant, _, _, _, _, factory, _ = await setup(integration_engine)
    service = CostingServiceImpl(factory, Phase1CostingAuthorizer(tenant))
    failure = OperationalError(
        "controlled statement", {}, Exception("controlled unavailable")
    )

    async def broken(*args):
        raise failure

    monkeypatch.setattr(CostSheetRepositoryImpl, "get", broken)
    with pytest.raises(OperationalError) as error:
        await service.get_sheet(tenant, CostSheetId(new_id("cost")), actor=BOSS)
    assert error.value is failure


@pytest.mark.parametrize("kind", ["policy", "fx"])
@pytest.mark.parametrize("cross_tenant", [False, True])
async def test_existing_t2_readers_use_typed_not_found_and_keep_validation_compatibility(
    integration_engine, kind, cross_tenant
):
    from domains.costing.schemas import QuoteFxCreate
    from domains.costing.service import CostingQuoteNotFoundError
    from shared.errors import ValidationError
    from tests.integration.test_costing_quote_evidence import policy

    service, tenant, _, artifact, actors, *_ = await setup(integration_engine)
    fx_id = "fx_missing"
    if cross_tenant:
        if kind == "policy":
            value = await service.confirm_policy(
                tenant, policy(artifact), actor=BOSS, idempotency_key="typed-policy"
            )
            assert await service.get_policy(tenant, None, actor=BOSS) == value
        else:
            command = QuoteFxCreate.model_validate(
                {
                    "base_currency": "USD",
                    "quote_currency": "EUR",
                    "rate": "0.90",
                    "observed_at": "2026-08-27T09:00:00Z",
                    "source_ref": artifact,
                }
            )
            value = await service.confirm_quote_fx(
                tenant, command, actor=BOSS, idempotency_key="typed-fx"
            )
            fx_id = value.fx_id
            assert await service.get_quote_fx(tenant, fx_id, actor=BOSS) == value
        tenant = TenantId(new_id("tn"))
        actors.tenant = tenant
    with pytest.raises(CostingQuoteNotFoundError) as error:
        if kind == "policy":
            await service.get_policy(tenant, None, actor=BOSS)
        else:
            await service.get_quote_fx(tenant, fx_id, actor=BOSS)
    assert isinstance(error.value, ValidationError)
    assert error.value.code == "record_not_found"
    assert str(error.value) == "成本报价记录不存在"


async def test_policy_only_future_confirmation_remains_a_missing_read(
    integration_engine,
):
    from datetime import timedelta

    from domains.costing.service import CostingQuoteNotFoundError
    from tests.integration.test_costing_quote_evidence import NOW, policy

    service, tenant, _, artifact, *_ = await setup(integration_engine)
    await service.confirm_policy(
        tenant,
        policy(artifact, effective_from=(NOW + timedelta(days=1)).isoformat()),
        actor=BOSS,
        idempotency_key="future-only",
    )
    with pytest.raises(CostingQuoteNotFoundError):
        await service.get_policy(tenant, None, actor=BOSS)


@pytest.mark.parametrize("kind", ["policy", "fx"])
async def test_t2_reader_sql_failure_is_not_typed_not_found(
    integration_engine, monkeypatch, kind
):
    from infra.db.repositories import costing_quote

    service, tenant, *_ = await setup(integration_engine)
    failure = OperationalError(
        "controlled statement", {}, Exception("controlled unavailable")
    )

    async def broken(*args):
        raise failure

    cls, method = (
        (costing_quote.PricingPolicyRepositoryImpl, "get_effective")
        if kind == "policy"
        else (costing_quote.QuoteFxRepositoryImpl, "get")
    )
    monkeypatch.setattr(cls, method, broken)
    with pytest.raises(OperationalError) as error:
        if kind == "policy":
            await service.get_policy(tenant, None, actor=BOSS)
        else:
            await service.get_quote_fx(tenant, "fx_missing", actor=BOSS)
    assert error.value is failure
