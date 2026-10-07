"""Sourcing handoff 到成本域的真实 PostgreSQL 事务与来源收敛测试。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import func, inspect, select, text
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
from infra.db.outbox import OutboxEventRow
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import (
    CostItemRow,
    CostSheetRow,
    SourcingCaseRow,
    SourcingReviewRow,
)
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
    _seed_additional_product,
    _seed_handoff_dependencies,
    _seed_need,
    _UnusedEvidenceReader,
)
from workflows.sourcing_case.application import SourcingCaseApplication

NOW = datetime(2026, 8, 31, 16, tzinfo=UTC)


class _TrackedCases:
    def __init__(self, delegate: Any, owner: Any) -> None:
        self._delegate = delegate
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def get_for_update(self, tenant_id, case_id):
        result = await self._delegate.get_for_update(tenant_id, case_id)
        self._owner.case_locked = True
        return result


class _BarrierReviews:
    def __init__(self, delegate: Any, owner: Any, barrier: asyncio.Barrier) -> None:
        self._delegate = delegate
        self._owner = owner
        self._barrier = barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def get_for_case(self, tenant_id, case_id):
        result = await self._delegate.get_for_case(tenant_id, case_id)
        if not self._owner.case_locked:
            await self._barrier.wait()
        return result


class _BarrierSourcingUnitOfWork(SqlAlchemySourcingUnitOfWork):
    """在旧实现读到空 Review 后同步；正确实现先持有 Case 行锁。"""

    def __init__(self, factory, tenant_id, barrier: asyncio.Barrier) -> None:
        super().__init__(factory, tenant_id)
        self._barrier = barrier
        self.case_locked = False

    async def __aenter__(self):
        entered = await super().__aenter__()
        self.cases = _TrackedCases(self.cases, self)
        self.reviews = _BarrierReviews(self.reviews, self, self._barrier)
        return entered


class _NoActiveRunEngine:
    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        return None

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"终态精确重放不得访问 Workflow Engine: {name}")


class _UnusedQuota:
    pass


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
        tenant_id,
        case_id,
        opportunity_id,
        expected_need_id=need_id,
        actor=system,
    )
    return service, system, finance, boss, snapshot


async def _prepare_ready_review_case(
    engine: AsyncEngine,
    *,
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
    opportunity_id: OpportunityId,
    product_id: ProductId,
) -> tuple[
    async_sessionmaker,
    SourcingServiceImpl,
    SourcingActor,
    SourcingActor,
    SourcingActor,
    Any,
    SourcingReviewCommand,
]:
    """准备一个未审核的 candidates_ready Case，供并发与原子门禁测试。"""

    await _seed_need(engine, tenant_id, need_id)
    evidence = await _seed_handoff_dependencies(
        engine, tenant_id, need_id, opportunity_id, product_id
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    system = SourcingActor("system-review", tenant_id, SourcingScope.SYSTEM, "system")
    product = SourcingActor(
        "employee-product", tenant_id, SourcingScope.TENANT, "product"
    )
    boss = SourcingActor("employee-boss", tenant_id, SourcingScope.TENANT, "boss")
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
            tenant_id, case_id, (product_id,), evidence
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
        reason="并发审核使用同一 canonical 内容",
        expected_case_version=ready.version,
    )
    return factory, service, system, product, boss, case_id, command


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
async def test_application_exact_review_replay_uses_terminal_case_without_active_run(
    integration_engine: AsyncEngine,
) -> None:
    """真实 canonical Review + handed Case 足以证明终态重放，无需活跃 Run。"""

    tenant = TenantId(new_id("tn"))
    service, _system, _finance, boss, snapshot = await _prepare_handoff(
        integration_engine,
        tenant_id=tenant,
        need_id=ValidatedNeedId(new_id("need")),
        opportunity_id=OpportunityId(new_id("opp")),
        product_id=ProductId(new_id("prd")),
    )
    canonical = await service.confirm_review(tenant, snapshot.review_id, actor=boss)
    command = SourcingReviewCommand(
        primary_option_id=canonical.primary_option_id,
        alternate_option_ids=canonical.alternate_option_ids,
        reason=canonical.reason,
        expected_case_version=canonical.expected_case_version,
    )
    application = SourcingCaseApplication(
        sourcing=service,
        quota=_UnusedQuota(),
        engine=_NoActiveRunEngine(),
    )

    replayed = await application.review(
        tenant,
        snapshot.case_id,
        command,
        request_id="terminal-exact-replay",
        actor=boss,
    )

    assert replayed == canonical


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
async def test_handoff_rejects_mismatched_expected_need_before_case_or_outbox_write(
    integration_engine: AsyncEngine,
) -> None:
    """串线 Run 的 Need 必须在 Case 锁内、任何交接副作用之前失败。"""

    tenant = TenantId(new_id("tn"))
    need = ValidatedNeedId(new_id("need"))
    opportunity = OpportunityId(new_id("opp"))
    product_id = ProductId(new_id("prd"))
    factory, service, system, product, boss, case_id, command = (
        await _prepare_ready_review_case(
            integration_engine,
            tenant_id=tenant,
            need_id=need,
            opportunity_id=opportunity,
            product_id=product_id,
        )
    )
    await service.review(tenant, case_id, command, actor=product)
    await service.review(tenant, case_id, command, actor=boss)

    with pytest.raises(ValidationError, match="Need"):
        await service.hand_to_costing(
            tenant,
            case_id,
            opportunity,
            expected_need_id=ValidatedNeedId("need-wrong-run"),
            actor=system,
        )

    async with factory() as session:
        row = await session.scalar(
            select(SourcingCaseRow).where(
                SourcingCaseRow.tenant_id == tenant,
                SourcingCaseRow.case_id == case_id,
            )
        )
        handoff_events = await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == tenant,
                OutboxEventRow.event_type == "SourcingCaseHandedToCosting",
            )
        )
    assert row is not None
    assert row.state == "candidates_ready"
    assert row.opportunity_id is None
    assert handoff_events == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["same_actor", "pending_vs_boss", "different"])
async def test_concurrent_first_reviews_serialize_on_case_and_converge(
    integration_engine: AsyncEngine,
    mode: str,
) -> None:
    """并发首写必须读到锁内 canonical Review，不能泄漏唯一键冲突或丢确认。"""

    tenant = TenantId(new_id("tn"))
    factory, _service, _system, product, boss, case_id, command = (
        await _prepare_ready_review_case(
            integration_engine,
            tenant_id=tenant,
            need_id=ValidatedNeedId(new_id("need")),
            opportunity_id=OpportunityId(new_id("opp")),
            product_id=ProductId(new_id("prd")),
        )
    )
    barrier = asyncio.Barrier(2)
    service = SourcingServiceImpl(
        lambda bound: _BarrierSourcingUnitOfWork(factory, bound, barrier),
        Phase2SourcingAuthorizer(tenant),
        _UnusedEvidenceReader(),
        now=lambda: NOW,
    )
    if mode == "same_actor":
        actors = (product, product)
        commands = (command, command)
    elif mode == "pending_vs_boss":
        actors = (product, boss)
        commands = (command, command)
    else:
        actors = (product, product)
        commands = (
            command,
            command.model_copy(update={"reason": "不同并发审核内容"}),
        )

    results = await asyncio.gather(
        service.review(tenant, case_id, commands[0], actor=actors[0]),
        service.review(tenant, case_id, commands[1], actor=actors[1]),
        return_exceptions=True,
    )

    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(SourcingReviewRow).where(
                        SourcingReviewRow.tenant_id == tenant,
                        SourcingReviewRow.case_id == case_id,
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1
    if mode == "same_actor":
        assert not any(isinstance(item, BaseException) for item in results)
        assert (
            cast(Any, results[0]).review_id
            == cast(Any, results[1]).review_id
            == rows[0].review_id
        )
    elif mode == "pending_vs_boss":
        assert not any(isinstance(item, BaseException) for item in results)
        assert rows[0].confirmed_by == boss.actor_id
        boss_result = cast(Any, results[1])
        assert boss_result.confirmed_by == boss.actor_id
    else:
        successes = [item for item in results if not isinstance(item, BaseException)]
        failures = [item for item in results if isinstance(item, BaseException)]
        assert len(successes) == len(failures) == 1
        assert isinstance(failures[0], ValidationError)
        assert rows[0].reason == cast(Any, successes[0]).reason


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
    assert sheets[0].source_tier_minimum_quantity == 1000
    assert sheets[0].source_unit == "piece"
    assert view.source_sourcing_case_id == snapshot.case_id
    assert view.source_option_id == snapshot.primary_option_id
    assert view.source_product_id == snapshot.product_id
    assert view.source_candidate_id is None
    assert view.source_tier_minimum_quantity == 1000
    assert view.source_unit == "piece"
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
        minimum_quantity=tier.minimum_quantity,
        unit_amount=tier.unit_amount,
        currency=tier.currency,
        unit=tier.unit,
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
        {"minimum_quantity": command.minimum_quantity + 1},
        {"unit_amount": Decimal("99.000000000001")},
        {"currency": "EUR"},
        {"unit": "carton"},
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
        minimum_quantity=other.price_options[0].minimum_quantity,
        unit_amount=other.price_options[0].unit_amount,
        currency=other.price_options[0].currency,
        unit=other.price_options[0].unit,
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


@pytest.mark.asyncio
async def test_cost_origin_composite_fk_rejects_real_option_with_unrelated_real_product(
    integration_engine: AsyncEngine,
) -> None:
    """独立存在的 Option A 与 Product B 不能拼成一条伪造成本来源。"""

    tenant = TenantId(new_id("tn"))
    product_a = ProductId(new_id("prd"))
    product_b = ProductId(new_id("prd"))
    _sourcing, _system, _finance, _boss, snapshot = await _prepare_handoff(
        integration_engine,
        tenant_id=tenant,
        need_id=ValidatedNeedId(new_id("need")),
        opportunity_id=OpportunityId(new_id("opp")),
        product_id=product_a,
    )
    await _seed_additional_product(integration_engine, tenant, product_b)

    async with integration_engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                item["name"] for item in inspect(sync).get_columns("cost_sheets")
            }
        )
        tier_columns = ""
        tier_values = ""
        if {"source_tier_minimum_quantity", "source_unit"} <= columns:
            tier_columns = ", source_tier_minimum_quantity, source_unit"
            tier_values = ", 1000, 'piece'"
        await connection.rollback()
        transaction = await connection.begin()
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(
                text(
                    "INSERT INTO cost_sheets "
                    "(tenant_id, cost_sheet_id, opportunity_id, version_type, "
                    "version_number, quantity, base_currency, quote_currency, created_at, "
                    "source_sourcing_case_id, source_option_id, source_product_id"
                    f"{tier_columns}) VALUES "
                    "(:tenant, :cost, :opportunity, 'estimated', 1, 5000, 'USD', "
                    "'USD', now(), :case, :option, :product"
                    f"{tier_values})"
                ),
                {
                    "tenant": tenant,
                    "cost": new_id("cost"),
                    "opportunity": snapshot.opportunity_id,
                    "case": snapshot.case_id,
                    "option": snapshot.primary_option_id,
                    "product": product_b,
                },
            )
        await transaction.rollback()
    assert "fk_cost_sheets_sourcing_option_product" in str(caught.value)
