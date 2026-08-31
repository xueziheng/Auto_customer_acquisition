"""Sourcing handoff 到成本域的真实 PostgreSQL 事务与来源收敛测试。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.costing.permissions import (
    CostingActor,
    CostingScope,
    Phase1CostingAuthorizer,
)
from domains.costing.service_impl import CostingServiceImpl
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.schemas import SourcingReviewCommand
from domains.sourcing.service_impl import SourcingServiceImpl
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import CostItemRow, CostSheetRow
from shared.errors import PermissionDenied, ValidationError
from shared.events.catalog import SourcingCaseHandedToCosting
from shared.schemas.identifiers import (
    CostSheetId,
    OpportunityId,
    ProductId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from tests.integration.test_sourcing_service_persistence import (
    _command,
    _qualified_product_ladder_check,
    _seed_handoff_dependencies,
    _seed_need,
    _UnusedEvidenceReader,
)

NOW = datetime(2026, 8, 31, 16, tzinfo=UTC)


def _symbol(module_name: str, symbol_name: str) -> Any:
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol_name):
        pytest.fail(f"{module_name}.{symbol_name} 尚未实现")
    return getattr(module, symbol_name)


async def _prepare_handoff(
    engine: AsyncEngine,
    *,
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
    opportunity_id: OpportunityId,
    product_id: ProductId,
    boss_first: bool = False,
) -> tuple[
    SourcingServiceImpl,
    SourcingActor,
    SourcingActor,
    SourcingActor,
    Any,
]:
    await _seed_need(engine, tenant_id, need_id)
    evidence_ref = await _seed_handoff_dependencies(
        engine, tenant_id, need_id, opportunity_id, product_id
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    system = SourcingActor("system-handoff", tenant_id, SourcingScope.SYSTEM, "system")
    sourcing_user = SourcingActor(
        "employee-sourcing", tenant_id, SourcingScope.TENANT, "sourcing"
    )
    boss = SourcingActor("employee-boss", tenant_id, SourcingScope.TENANT, "boss")
    finance = SourcingActor(
        "employee-finance", tenant_id, SourcingScope.TENANT, "finance"
    )
    service = SourcingServiceImpl(
        lambda bound: SqlAlchemySourcingUnitOfWork(factory, bound),
        Phase2SourcingAuthorizer(tenant_id),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    case_id = await service.open_case(
        tenant_id, _command(tenant_id, need_id), actor=system
    )
    await service.record_ladder_check(
        tenant_id,
        case_id,
        _qualified_product_ladder_check(
            tenant_id, case_id, (product_id,), evidence_ref
        ),
        actor=system,
    )
    option_id = await service.register_existing_product_option(
        tenant_id, case_id, product_id, actor=system
    )
    await service.mark_candidates_ready(
        tenant_id, case_id, (option_id,), (), actor=system
    )
    async with SqlAlchemySourcingUnitOfWork(factory, tenant_id) as uow:
        ready = await uow.cases.get(tenant_id, case_id)
    assert ready is not None
    command = SourcingReviewCommand(
        primary_option_id=option_id,
        alternate_option_ids=(),
        reason="内部成本来源完整",
        expected_case_version=ready.version,
    )
    pending = (
        await service.review(tenant_id, case_id, command, actor=sourcing_user)
        if not boss_first
        else None
    )
    confirmed = await service.review(
        tenant_id,
        case_id,
        command,
        actor=boss,
    )
    if pending is not None:
        assert confirmed.review_id == pending.review_id
    else:
        assert confirmed.submitted_by == confirmed.confirmed_by == boss.actor_id
        assert confirmed.submitted_at == confirmed.confirmed_at == NOW
    snapshot = await service.hand_to_costing(
        tenant_id, case_id, opportunity_id, actor=system
    )
    return service, system, finance, boss, snapshot


@pytest.mark.asyncio
async def test_boss_first_review_is_created_and_confirmed_atomically(
    integration_engine: AsyncEngine,
) -> None:
    """老板首次 review 必须在同一事实中创建并确认，不能留下瞬时 pending。"""

    tenant = TenantId(new_id("tn"))
    service, _system, _finance, boss, snapshot = await _prepare_handoff(
        integration_engine,
        tenant_id=tenant,
        need_id=ValidatedNeedId(new_id("need")),
        opportunity_id=OpportunityId(new_id("opp")),
        product_id=ProductId(new_id("prd")),
        boss_first=True,
    )
    replayed = await service.confirm_review(
        tenant, snapshot.review_id, actor=boss
    )
    assert replayed.confirmed_by == boss.actor_id
    assert replayed.confirmed_at == NOW


@pytest.mark.asyncio
async def test_review_submit_and_boss_confirmation_are_separate_atomic_facts(
    integration_engine: AsyncEngine,
) -> None:
    """普通提交不得自批；老板精确重交确认且不改写首次人工事实。"""

    tenant = TenantId(new_id("tn"))
    need = ValidatedNeedId(new_id("need"))
    opportunity = OpportunityId(new_id("opp"))
    product = ProductId(new_id("prd"))
    await _seed_need(integration_engine, tenant, need)
    evidence = await _seed_handoff_dependencies(
        integration_engine, tenant, need, opportunity, product
    )
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    service = SourcingServiceImpl(
        lambda bound: SqlAlchemySourcingUnitOfWork(factory, bound),
        Phase2SourcingAuthorizer(tenant),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    system = SourcingActor("system-review", tenant, SourcingScope.SYSTEM, "system")
    product_user = SourcingActor(
        "employee-product", tenant, SourcingScope.TENANT, "product"
    )
    boss = SourcingActor("employee-boss", tenant, SourcingScope.TENANT, "boss")
    case_id = await service.open_case(tenant, _command(tenant, need), actor=system)
    await service.record_ladder_check(
        tenant,
        case_id,
        _qualified_product_ladder_check(tenant, case_id, (product,), evidence),
        actor=system,
    )
    option = await service.register_existing_product_option(
        tenant, case_id, product, actor=system
    )
    await service.mark_candidates_ready(tenant, case_id, (option,), (), actor=system)
    async with SqlAlchemySourcingUnitOfWork(factory, tenant) as uow:
        ready = await uow.cases.get(tenant, case_id)
    assert ready is not None
    command = SourcingReviewCommand(
        primary_option_id=option,
        alternate_option_ids=(),
        reason="选择证据完整的主供给",
        expected_case_version=ready.version,
    )

    pending = await service.review(tenant, case_id, command, actor=product_user)
    assert pending.confirmed_by is None and pending.confirmed_at is None
    confirmed = await service.review(tenant, case_id, command, actor=boss)
    replayed = await service.confirm_review(
        tenant, confirmed.review_id, actor=boss
    )

    assert confirmed.review_id == pending.review_id == replayed.review_id
    assert confirmed.submitted_by == pending.submitted_by
    assert confirmed.submitted_at == pending.submitted_at
    assert confirmed.confirmed_by == "employee-boss"
    assert replayed.confirmed_at == confirmed.confirmed_at == NOW
    with pytest.raises(ValidationError, match="已有审核事实"):
        await service.review(
            tenant,
            case_id,
            command.model_copy(update={"reason": "不同选择理由"}),
            actor=boss,
        )
    with pytest.raises(PermissionDenied):
        await service.confirm_review(
            tenant, confirmed.review_id, actor=system
        )


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_handoff_converge_to_one_estimated_primary_cost(
    integration_engine: AsyncEngine,
) -> None:
    """重复 Outbox 投递和并发 worker 只能留下一个来源表和一个采购成本项。"""

    handler_type = _symbol(
        "apps.scheduler_worker.sourcing_costing", "SourcingCostHandoffHandler"
    )
    tenant = TenantId(new_id("tn"))
    need = ValidatedNeedId(new_id("need"))
    opportunity = OpportunityId(new_id("opp"))
    product = ProductId(new_id("prd"))
    sourcing, _system, finance, _boss, snapshot = await _prepare_handoff(
        integration_engine,
        tenant_id=tenant,
        need_id=need,
        opportunity_id=opportunity,
        product_id=product,
    )
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    costing = CostingServiceImpl(
        lambda bound: SqlAlchemyCostingUnitOfWork(factory, bound),
        Phase1CostingAuthorizer(tenant),
        now=lambda: NOW,
    )
    costing_actor = CostingActor(
        "system:sourcing-cost", "system", CostingScope.SYSTEM, tenant
    )
    handler = handler_type(
        sourcing=sourcing,
        costing=costing,
        tenant_id=tenant,
        sourcing_actor=finance,
        costing_actor=costing_actor,
    )
    event = SourcingCaseHandedToCosting(
        tenant_id=tenant,
        occurred_at=NOW,
        case_id=snapshot.case_id,
        need_id=snapshot.need_id,
        opportunity_id=snapshot.opportunity_id,
        review_id=snapshot.review_id,
    )

    await asyncio.gather(handler.handle(event), handler.handle(event))
    await handler.handle(event)

    async with factory() as session:
        sheets = list(
            (
                await session.execute(
                    select(CostSheetRow).where(
                        CostSheetRow.tenant_id == tenant,
                        CostSheetRow.source_sourcing_case_id == snapshot.case_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        items = list(
            (
                await session.execute(
                    select(CostItemRow).where(
                        CostItemRow.tenant_id == tenant,
                        CostItemRow.cost_sheet_id == sheets[0].cost_sheet_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(sheets) == 1
    view = await costing.get_sheet(
        tenant,
        CostSheetId(sheets[0].cost_sheet_id),
        actor=CostingActor(
            "employee-finance", "finance", CostingScope.TENANT
        ),
    )
    assert sheets[0].version_type == "estimated"
    assert sheets[0].source_option_id == snapshot.primary_option_id
    assert sheets[0].source_product_id == snapshot.product_id
    assert sheets[0].source_candidate_id is None
    assert view.source_sourcing_case_id == snapshot.case_id
    assert view.source_option_id == snapshot.primary_option_id
    assert view.source_product_id == snapshot.product_id
    assert view.source_candidate_id is None
    assert len(items) == 1
    assert items[0].item_type == "product_purchase"
    assert items[0].price_basis == "indicative"
    assert items[0].is_per_unit is True
    assert Decimal(items[0].amount) == snapshot.price_options[0].unit_amount
    assert items[0].currency == snapshot.price_options[0].currency


@pytest.mark.asyncio
async def test_source_content_mismatch_rejects_and_failed_insert_rolls_back(
    integration_engine: AsyncEngine,
) -> None:
    """来源键不能掩盖内容漂移，FK 失败也不能留下半张表或半条成本项。"""

    command_type = _symbol("domains.costing.schemas", "SourcingEstimateCreate")
    tenant = TenantId(new_id("tn"))
    need = ValidatedNeedId(new_id("need"))
    opportunity = OpportunityId(new_id("opp"))
    product = ProductId(new_id("prd"))
    _sourcing, _system, _finance, _boss, snapshot = await _prepare_handoff(
        integration_engine,
        tenant_id=tenant,
        need_id=need,
        opportunity_id=opportunity,
        product_id=product,
    )
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    service = CostingServiceImpl(
        lambda bound: SqlAlchemyCostingUnitOfWork(factory, bound),
        Phase1CostingAuthorizer(tenant),
        now=lambda: NOW,
    )
    actor = CostingActor(
        "system:sourcing-cost", "system", CostingScope.SYSTEM, tenant
    )
    tier = snapshot.price_options[0]
    command = command_type(
        sourcing_case_id=snapshot.case_id,
        primary_option_id=snapshot.primary_option_id,
        supplier_candidate_id=snapshot.supplier_candidate_id,
        product_id=snapshot.product_id,
        opportunity_id=snapshot.opportunity_id,
        quantity=snapshot.quantity,
        unit_amount=tier.unit_amount,
        currency=tier.currency,
        evidence_ref=tier.evidence_ref,
    )
    first = await service.create_sourcing_estimate(tenant, command, actor=actor)
    assert await service.create_sourcing_estimate(tenant, command, actor=actor) == first

    mutations = (
        {"opportunity_id": OpportunityId("opp-mismatch")},
        {"primary_option_id": "sop-mismatch"},
        {"product_id": ProductId("prd-mismatch")},
        {"supplier_candidate_id": SupplierCandidateId("spc-mismatch")},
        {"quantity": command.quantity + 1},
        {"unit_amount": Decimal("99.000000000001")},
        {"currency": "EUR"},
        {"evidence_ref": "art_mismatch"},
    )
    for values in mutations:
        with pytest.raises(ValidationError, match="来源内容不一致"):
            await service.create_sourcing_estimate(
                tenant, command.model_copy(update=values), actor=actor
            )

    other_tenant = TenantId(new_id("tn"))
    other_need = ValidatedNeedId(new_id("need"))
    other_opportunity = OpportunityId(new_id("opp"))
    other_product = ProductId(new_id("prd"))
    _other_sourcing, _s, _f, _b, other = await _prepare_handoff(
        integration_engine,
        tenant_id=other_tenant,
        need_id=other_need,
        opportunity_id=other_opportunity,
        product_id=other_product,
    )
    bad = command_type(
        sourcing_case_id=other.case_id,
        primary_option_id=other.primary_option_id,
        supplier_candidate_id=other.supplier_candidate_id,
        product_id=other.product_id,
        opportunity_id=OpportunityId("opp-does-not-exist"),
        quantity=other.quantity,
        unit_amount=other.price_options[0].unit_amount,
        currency=other.price_options[0].currency,
        evidence_ref=other.price_options[0].evidence_ref,
    )
    other_service = CostingServiceImpl(
        lambda bound: SqlAlchemyCostingUnitOfWork(factory, bound),
        Phase1CostingAuthorizer(other_tenant),
        now=lambda: NOW,
    )
    other_actor = CostingActor(
        "system:sourcing-cost", "system", CostingScope.SYSTEM, other_tenant
    )
    with pytest.raises(IntegrityError):
        await other_service.create_sourcing_estimate(
            other_tenant, bad, actor=other_actor
        )

    async with factory() as session:
        failed_sheets = await session.scalar(
            select(func.count())
            .select_from(CostSheetRow)
            .where(
                CostSheetRow.tenant_id == other_tenant,
                CostSheetRow.source_sourcing_case_id == other.case_id,
            )
        )
        failed_items = await session.scalar(
            select(func.count())
            .select_from(CostItemRow)
            .join(
                CostSheetRow,
                (CostSheetRow.tenant_id == CostItemRow.tenant_id)
                & (CostSheetRow.cost_sheet_id == CostItemRow.cost_sheet_id),
            )
            .where(
                CostSheetRow.tenant_id == other_tenant,
                CostSheetRow.source_sourcing_case_id == other.case_id,
            )
        )
    assert failed_sheets == failed_items == 0
