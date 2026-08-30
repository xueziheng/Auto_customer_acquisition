"""需求首次跨越寻源门槛时的 outbox 事实事件集成测试。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.service import DemandService
from shared.schemas.identifiers import TenantId, new_id

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture
async def demand_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> DemandService:
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    service_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return service_type(lambda requested: uow_type(factory, requested, now=clock.now), now=clock.now)


async def _promote_need(
    service: DemandService,
    tenant: TenantId,
    fields: dict[str, object],
) -> str:
    signal_id = await service.capture_signal(
        tenant,
        importlib.import_module("domains.demand.schemas").SignalCaptureRequest(
            signal_type="inbound_inquiry",
            entity_name="Acme Manufacturing",
            raw_observation="Customer requested marine hinges.",
            observed_at=NOW,
            source_type="conversation",
            source_id="msg_customer_interest",
            extracted_by="model-v1",
        ),
    )
    models = importlib.import_module("domains.demand.models")
    hypothesis_id = await service.create_hypothesis(
        tenant,
        models.ProspectAccountId(new_id("acc")),
        "marine hinges",
        [signal_id],
        "客户询问了海用铰链。",
        "model-v1",
    )
    return await service.promote_to_validated(
        tenant, hypothesis_id, "msg_customer_interest", fields
    )


async def _outbox_types(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[str]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(tables.OutboxEventRow.event_type)
                    .where(tables.OutboxEventRow.tenant_id == str(tenant))
                    .order_by(tables.OutboxEventRow.published_at)
                )
            ).all()
        )


async def test_initial_sourcing_ready_promotion_only_publishes_need_validated(
    demand_db: AsyncEngine,
) -> None:
    """初次晋升已经由 NeedValidated 覆盖，不重发 readiness。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _service(factory, tenant, MutableClock(NOW))

    await _promote_need(
        service,
        tenant,
        {"product_category": "hinges", "application": "marine", "quantity": 500},
    )

    event_types = await _outbox_types(factory, tenant)
    assert event_types.count("NeedValidated") == 1
    assert "NeedBecameSourcingReady" not in event_types


async def test_later_first_transition_publishes_one_readiness_fact(
    demand_db: AsyncEngine,
) -> None:
    """完整度从 2 到 3 时只持久一次 readiness；重复更新/标记均不重复。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _service(factory, tenant, MutableClock(NOW))
    need_id = await _promote_need(
        service, tenant, {"product_category": "hinges", "application": "marine"}
    )

    await service.update_need_fields(
        tenant, need_id, {"quantity": 500}, "msg_quantity", None
    )
    await service.update_need_fields(
        tenant, need_id, {"quantity": 500}, "msg_quantity", None
    )
    await service.mark_sourcing_ready(tenant, need_id)

    event_types = await _outbox_types(factory, tenant)
    assert event_types.count("NeedValidated") == 1
    assert event_types.count("NeedBecameSourcingReady") == 1
