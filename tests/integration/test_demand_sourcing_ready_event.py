"""需求首次跨越寻源门槛时的 outbox 事实事件集成测试。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.service import DemandService
from shared.schemas.identifiers import TenantId, ValidatedNeedId, new_id

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
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    clock: MutableClock,
    *,
    account_names: object | None = None,
) -> DemandService:
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    service_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return service_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
        account_names=account_names,
    )


class _Countries:
    """归簇仅需租户内企业国家，不跨域读取真实 Prospecting。"""

    async def names_for(self, tenant_id: TenantId, account_ids: tuple[object, ...]) -> dict[object, str]:
        return {account_id: "Acme Manufacturing" for account_id in account_ids}

    async def countries_for(self, tenant_id: TenantId, account_ids: tuple[object, ...]) -> dict[object, str]:
        return {account_id: "US" for account_id in account_ids}

    async def domains_for(self, tenant_id: TenantId, account_ids: tuple[object, ...]) -> dict[object, str]:
        return {account_id: "acme.example" for account_id in account_ids}


class _ReadinessPublicationFailed(RuntimeError):
    """测试中模拟 outbox 行已加入 session 后的发布失败。"""


class _FailAfterReadinessPublishBus:
    """保留真实 outbox 写入，再在 UoW 提交前注入可观察失败。"""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def publish(self, event: object) -> None:
        await self._inner.publish(event)
        if type(event).__name__ == "NeedBecameSourcingReady":
            raise _ReadinessPublicationFailed("force readiness publish failure")


class _FailAfterReadinessPublishUnitOfWork:
    """使用真实 demand UoW/仓储，仅在 readiness 写入后中断提交。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant: TenantId,
        clock: MutableClock,
    ) -> None:
        uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
        self._inner = uow_type(factory, tenant, now=clock.now)

    async def __aenter__(self) -> Self:
        inner = await self._inner.__aenter__()
        self.needs = inner.needs
        self.bus = _FailAfterReadinessPublishBus(inner.bus)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc, tb)


def _failing_service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> DemandService:
    service_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return service_type(
        lambda requested: _FailAfterReadinessPublishUnitOfWork(
            factory, requested, clock
        ),
        now=clock.now,
    )


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


async def _outbox_payloads(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, event_type: str
) -> list[dict[str, object]]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(tables.OutboxEventRow.event_payload)
                    .where(
                        tables.OutboxEventRow.tenant_id == str(tenant),
                        tables.OutboxEventRow.event_type == event_type,
                    )
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


async def test_readiness_publication_failure_rolls_back_need_and_outbox(
    demand_db: AsyncEngine,
) -> None:
    """事件加入真实 outbox 后失败时，需求跨门槛写入必须一起回滚。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    need_id = await _promote_need(
        _service(factory, tenant, clock),
        tenant,
        {"product_category": "hinges", "application": "marine"},
    )

    with pytest.raises(_ReadinessPublicationFailed):
        await _failing_service(factory, tenant, clock).update_need_fields(
            tenant, need_id, {"quantity": 500}, "msg_quantity", None
        )

    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    async with uow_type(factory, tenant, now=clock.now) as uow:
        persisted_need = await uow.needs.get(tenant, ValidatedNeedId(need_id))
    assert persisted_need is not None
    assert persisted_need.status.value == "validated"
    assert persisted_need.completeness == 2
    assert persisted_need.quantity is None
    assert "NeedBecameSourcingReady" not in await _outbox_types(factory, tenant)


async def test_cluster_assignment_publishes_membership_facts_once_per_need(
    demand_db: AsyncEngine,
) -> None:
    """首次归簇均写成员变更事实；第二成员仍是唯一的 formed 时点。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _service(
        factory,
        tenant,
        MutableClock(NOW),
        account_names=_Countries(),
    )
    first_need_id = await _promote_need(
        service,
        tenant,
        {"product_category": "hinges", "application": "marine", "material": "stainless"},
    )
    second_need_id = await _promote_need(
        service,
        tenant,
        {"product_category": "hinges", "application": "marine", "material": "stainless"},
    )

    first_cluster_id = await service.try_assign_cluster(tenant, ValidatedNeedId(first_need_id))
    second_cluster_id = await service.try_assign_cluster(tenant, ValidatedNeedId(second_need_id))
    duplicate_cluster_id = await service.try_assign_cluster(tenant, ValidatedNeedId(second_need_id))

    membership_payloads = await _outbox_payloads(
        factory, tenant, "NeedClusterMembershipChanged"
    )
    assert first_cluster_id is not None
    assert second_cluster_id == first_cluster_id == duplicate_cluster_id
    assert len(membership_payloads) == 2
    membership_by_need = {
        payload["changed_need_id"]: payload for payload in membership_payloads
    }
    assert len(membership_by_need) == 2
    assert membership_by_need[first_need_id]["member_count"] == 1
    assert membership_by_need[second_need_id]["member_count"] == 2
    formed_payloads = await _outbox_payloads(factory, tenant, "NeedClusterFormed")
    assert len(formed_payloads) == 1
    assert formed_payloads[0]["cluster_id"] == first_cluster_id
    assert formed_payloads[0]["member_count"] == 2


async def test_priority_facts_change_only_when_cluster_membership_version_changes(
    demand_db: AsyncEngine,
) -> None:
    """推进读时钟不改事实；新增成员只推进一次 cluster 版本与成员数。"""

    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock, account_names=_Countries())
    first_need_id = ValidatedNeedId(
        await _promote_need(
            service,
            tenant,
            {
                "product_category": "hinges",
                "application": "marine",
                "material": "stainless",
            },
        )
    )
    cluster_id = await service.try_assign_cluster(tenant, first_need_id)
    assert cluster_id is not None
    initial = await service.get_cluster_priority_facts(tenant, first_need_id)

    clock.value = NOW + timedelta(hours=1)
    repeated = await service.get_cluster_priority_facts(tenant, first_need_id)

    assert repeated == initial
    assert initial.cluster_member_count == 1
    assert initial.facts_observed_at == NOW

    second_need_id = ValidatedNeedId(
        await _promote_need(
            service,
            tenant,
            {
                "product_category": "hinges",
                "application": "marine",
                "material": "stainless",
            },
        )
    )
    assert await service.try_assign_cluster(tenant, second_need_id) == cluster_id
    changed = await service.get_cluster_priority_facts(tenant, first_need_id)

    assert changed.cluster_member_count == 2
    assert changed.facts_observed_at == NOW + timedelta(hours=1)
    assert changed != initial

    clock.value = NOW + timedelta(days=1)
    assert await service.get_cluster_priority_facts(tenant, first_need_id) == changed
