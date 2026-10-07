"""需求首次跨越寻源门槛时的 outbox 事实事件集成测试。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.service import DemandService
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, ValidatedNeedId, new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- 每例隔离真实 PostgreSQL
)

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture
async def demand_db(unit_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """复购与归簇事实必须在每例独立数据库验证，避免污染迁移保护测试。"""

    yield unit_engine


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


class _FailAfterCatalogPublishBus:
    """先加入真实 outbox，再模拟目录事实发布路径失败。"""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def publish(self, event: object) -> None:
        await self._inner.publish(event)
        if type(event).__name__ == "NeedCatalogFactsChanged":
            raise _ReadinessPublicationFailed("force catalog publish failure")


class _FailAfterCatalogPublishUnitOfWork:
    """保留真实仓储/事务，只在目录事实 outbox 写入后中断。"""

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
        self.bus = _FailAfterCatalogPublishBus(inner.bus)
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


def _failing_catalog_service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> DemandService:
    service_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return service_type(
        lambda requested: _FailAfterCatalogPublishUnitOfWork(
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
    catalog_payloads = await _outbox_payloads(
        factory, tenant, "NeedCatalogFactsChanged"
    )
    assert catalog_payloads == [
        {
            "tenant_id": str(tenant),
            "occurred_at": NOW.isoformat(),
            "run_id": None,
            "need_id": str(need_id),
            "cluster_id": None,
            "change_kind": "quantity",
        }
    ]


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


async def test_recurring_requirement_roundtrips_history_and_is_idempotent(
    demand_db: AsyncEngine,
) -> None:
    """False/True/缺失保持三态；相同消息重放不追加历史或事件。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    need_id = await _promote_need(
        service,
        tenant,
        {
            "product_category": "hinges",
            "application": "marine",
            "recurring_requirement": False,
        },
    )
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    async with uow_type(factory, tenant, now=clock.now) as uow:
        initial = await uow.needs.get(tenant, ValidatedNeedId(need_id))
    assert initial is not None and initial.recurring_requirement is not None
    assert initial.recurring_requirement.value is False
    assert initial.completeness == 2
    assert initial.status.value == "validated"
    assert await _outbox_payloads(factory, tenant, "NeedCatalogFactsChanged") == []

    await service.update_need_fields(
        tenant,
        need_id,
        {
            "recurring_requirement": {
                "value": True,
                "quote": "We reorder these every quarter.",
                "extracted_by": "reply-model-v3",
            }
        },
        "msg_recurring_2",
        "emp-1",
    )
    await service.update_need_fields(
        tenant,
        need_id,
        {
            "recurring_requirement": {
                "value": True,
                "quote": "We reorder these every quarter.",
                "extracted_by": "reply-model-v3",
            }
        },
        "msg_recurring_2",
        "emp-1",
    )

    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = await session.get(
            tables.ValidatedNeedRow, (str(tenant), str(need_id))
        )
        history = (
            await session.scalars(
                select(tables.ValidatedNeedFieldHistoryRow).where(
                    tables.ValidatedNeedFieldHistoryRow.tenant_id == str(tenant),
                    tables.ValidatedNeedFieldHistoryRow.need_id == str(need_id),
                    tables.ValidatedNeedFieldHistoryRow.field_name
                    == "recurring_requirement",
                )
            )
        ).all()
    assert row is not None
    assert row.recurring_requirement["value"] is True
    assert row.status == "validated"
    assert len(history) == 1
    assert history[0].old_value == "False"
    assert history[0].new_value == "True"
    payloads = await _outbox_payloads(factory, tenant, "NeedCatalogFactsChanged")
    assert payloads == [
        {
            "tenant_id": str(tenant),
            "occurred_at": NOW.isoformat(),
            "run_id": None,
            "need_id": str(need_id),
            "cluster_id": None,
            "change_kind": "recurring_requirement",
        }
    ]
    assert "NeedBecameSourcingReady" not in await _outbox_types(factory, tenant)


@pytest.mark.parametrize("value", ["true", "false", "True", "False"])
async def test_recurring_requirement_update_rejects_bool_like_strings(
    demand_db: AsyncEngine, value: str
) -> None:
    """字符串不得经 Python truthiness 或自定义转换成为 recurrence。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _service(factory, tenant, MutableClock(NOW))
    need_id = await _promote_need(
        service, tenant, {"product_category": "hinges", "application": "marine"}
    )

    with pytest.raises(ValidationError, match="需求字段类型无效"):
        await service.update_need_fields(
            tenant,
            need_id,
            {"recurring_requirement": value},
            "msg_recurrence",
            None,
        )


async def test_recurring_requirement_update_rejects_missing_source_message(
    demand_db: AsyncEngine,
) -> None:
    """删除来源消息前置校验会写入无法定位客户证据的 recurrence。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _service(factory, tenant, MutableClock(NOW))
    need_id = await _promote_need(
        service, tenant, {"product_category": "hinges", "application": "marine"}
    )

    with pytest.raises(ValidationError, match="来源消息无效"):
        await service.update_need_fields(
            tenant,
            need_id,
            {"recurring_requirement": True},
            "",
            None,
        )


async def test_recurring_requirement_update_preserves_existing_need_state(
    demand_db: AsyncEngine,
) -> None:
    """recurrence 不能借既有完整度修正或推进当前 Need 状态。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    need_id = await _promote_need(
        service,
        tenant,
        {"product_category": "hinges", "application": "marine", "quantity": 500},
    )
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    models = importlib.import_module("domains.demand.models")
    async with uow_type(factory, tenant, now=clock.now) as uow:
        need = await uow.needs.get_for_update(tenant, ValidatedNeedId(need_id))
        assert need is not None
        await uow.needs.update(replace(need, status=models.NeedStatus.VALIDATED))

    await service.update_need_fields(
        tenant,
        need_id,
        {"recurring_requirement": True},
        "msg_recurring_state",
        "emp-1",
    )

    async with uow_type(factory, tenant, now=clock.now) as uow:
        persisted = await uow.needs.get(tenant, ValidatedNeedId(need_id))
    assert persisted is not None
    assert persisted.completeness == 3
    assert persisted.status is models.NeedStatus.VALIDATED


async def test_catalog_publication_failure_rolls_back_recurrence_and_outbox(
    demand_db: AsyncEngine,
) -> None:
    """目录事件写入后失败必须同时回滚 fact、history 与 outbox。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    need_id = await _promote_need(
        _service(factory, tenant, clock),
        tenant,
        {"product_category": "hinges", "application": "marine"},
    )

    with pytest.raises(_ReadinessPublicationFailed):
        await _failing_catalog_service(factory, tenant, clock).update_need_fields(
            tenant,
            need_id,
            {"recurring_requirement": True},
            "msg_recurring_failure",
            "emp-1",
        )

    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    async with uow_type(factory, tenant, now=clock.now) as uow:
        persisted = await uow.needs.get(tenant, ValidatedNeedId(need_id))
    assert persisted is not None
    assert persisted.recurring_requirement is None
    assert await _outbox_payloads(factory, tenant, "NeedCatalogFactsChanged") == []


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

    await service.update_need_fields(
        tenant,
        second_need_id,
        {"quantity": 250},
        "msg_clustered_quantity",
        None,
    )
    catalog_payloads = await _outbox_payloads(
        factory, tenant, "NeedCatalogFactsChanged"
    )
    assert len(catalog_payloads) == 1
    assert catalog_payloads[0] == {
        "tenant_id": str(tenant),
        "occurred_at": NOW.isoformat(),
        "run_id": None,
        "need_id": str(second_need_id),
        "cluster_id": str(first_cluster_id),
        "change_kind": "quantity",
    }


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
