"""NeedHypothesis 与 ValidatedNeed 仓储集成测试（真实 PostgreSQL）。"""

from __future__ import annotations

import asyncio
import importlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.money import Money
from shared.schemas.provenance import SourceType

_models = importlib.import_module("domains.demand.models")

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
OBSERVATION_MARKER = "Acme SECRET-OBSERVATION-77 opened a new plant in Rotterdam."


@dataclass
class MutableClock:
    value: datetime
    calls: int = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.value


@pytest_asyncio.fixture
async def demand_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _uow_type():
    return importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> DemandService:
    impl_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return impl_type(
        lambda requested: _uow_type()(factory, requested, now=clock.now),
        now=clock.now,
    )


def _request(**overrides: object) -> SignalCaptureRequest:
    fields: dict[str, object] = {
        "signal_type": "inbound_inquiry",
        "entity_name": "Acme Manufacturing",
        "raw_observation": OBSERVATION_MARKER,
        "observed_at": NOW,
        "source_type": "conversation",
        "source_id": "msg_conv_001",
        "extracted_by": "model-v1",
    }
    fields.update(overrides)
    return SignalCaptureRequest(**fields)


async def _signal(
    service: DemandService, tenant: TenantId, **overrides: object
) -> str:
    return await service.capture_signal(tenant, _request(**overrides))


async def _hypothesis_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.NeedHypothesisRow).where(
                    tables.NeedHypothesisRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def _outbox_events(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.OutboxEventRow)
                .where(tables.OutboxEventRow.tenant_id == str(tenant))
                .order_by(
                    tables.OutboxEventRow.published_at,
                    tables.OutboxEventRow.event_id,
                )
            )
        ).scalars().all()
    return list(rows)


def _hypothesis(**overrides: object):
    fields: dict[str, object] = {
        "hypothesis_id": _models.NeedHypothesisId(new_id("hyp")),
        "tenant_id": TenantId(new_id("tn")),
        "account_id": _models.ProspectAccountId(new_id("acc")),
        "category": "stainless steel hinges",
        "reasoning": _models.InferredField(
            value="可能需要耐腐蚀五金",
            based_on=[
                _models.EvidenceItem(
                    level=_models.EvidenceLevel.PUBLIC_COMPANY_EVENT,
                    source_type="web_page",
                    source_id="sha256:pagehash001",
                    observed_at=NOW,
                    summary="Acme 扩建公告",
                )
            ],
            inferred_by="model-v1",
            inferred_at=NOW,
        ),
        "signal_ids": [_models.DemandSignalId(new_id("sig"))],
        "created_at": NOW,
    }
    fields.update(overrides)
    return _models.NeedHypothesis(**fields)


def _provenance(source_id: str = "msg_conv_001"):
    return _models.Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=source_id,
        extracted_by="human",
        extracted_at=NOW,
    )


def _factual(value: object, source_id: str = "msg_conv_001"):
    return _models.FactualField(value=value, provenance=_provenance(source_id))


def _need(tenant: TenantId, **overrides: object):
    fields: dict[str, object] = {
        "need_id": _models.ValidatedNeedId(new_id("need")),
        "tenant_id": tenant,
        "account_id": _models.ProspectAccountId(new_id("acc")),
        "product_category": _factual("hinges"),
        "source_message_id": _models.MessageId("msg_conv_001"),
        "source_conversation_id": _models.ConversationId("conv_001"),
        "created_at": NOW,
        "application": _factual("marine cabinet"),
        "material": _factual("316 stainless steel"),
        "size_spec": _factual("100 mm"),
        "quantity": _factual(5000, "msg_conv_002"),
        "packaging": _factual("20 pcs/carton"),
        "destination": _factual("Rotterdam"),
        "required_by": _factual(date(2026, 12, 31), "msg_conv_002"),
        "target_price": _factual(Money(Decimal("1.25"), "USD")),
        "current_supply_issue": _factual("corrosion failures"),
        "certification_required": _factual("REACH"),
    }
    fields.update(overrides)
    return _models.ValidatedNeed(**fields)


async def test_repo_hypothesis_jsonb_roundtrip(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    hypothesis = _hypothesis(tenant_id=tenant)
    async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
        assert await uow.hypotheses.add(hypothesis) is True
        loaded = await uow.hypotheses.get(tenant, hypothesis.hypothesis_id)
        assert loaded is not None
        assert loaded.reasoning == hypothesis.reasoning
        assert loaded.signal_ids == hypothesis.signal_ids
        assert loaded.status is _models.HypothesisStatus.INFERRED


def test_repository_conflict_predicate_covers_both_active_statuses() -> None:
    """仓储 ON CONFLICT 谓词必须与 0022 活跃唯一索引语义一致。"""
    repository = importlib.import_module("infra.db.repositories.need_hypotheses")
    statement = repository._build_hypothesis_insert(_hypothesis())
    sql = str(statement.compile(dialect=postgresql.dialect()))
    match = re.search(r"ON CONFLICT .*? WHERE (.*?) DO NOTHING", sql, re.DOTALL)
    assert match is not None
    predicate = match.group(1)
    assert frozenset(re.findall(r"'([^']*)'", predicate)) == frozenset(
        {"inferred", "contacting"}
    )
    assert "status" in predicate


async def test_repo_active_conflict_and_lists_filter_sort(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = _models.ProspectAccountId(new_id("acc"))
    first = _hypothesis(tenant_id=tenant, account_id=account)
    duplicate = _hypothesis(
        tenant_id=tenant,
        account_id=account,
        category=first.category,
        status=_models.HypothesisStatus.CONTACTING,
    )
    rejected = _hypothesis(
        tenant_id=tenant,
        account_id=account,
        category="aluminum profiles",
        status=_models.HypothesisStatus.REJECTED,
        rejection_reason="no_budget",
    )
    ready_old = _need(
        tenant,
        account_id=account,
        status=_models.NeedStatus.SOURCING_READY,
        created_at=NOW,
    )
    ready_new = _need(
        tenant,
        account_id=account,
        status=_models.NeedStatus.SOURCING_READY,
        created_at=datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
    )
    async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
        assert await uow.hypotheses.add(first) is True
        assert await uow.hypotheses.add(duplicate) is False
        assert await uow.hypotheses.add(rejected) is True
        await uow.needs.add(ready_new)
        await uow.needs.add(ready_old)
    async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
        active = await uow.hypotheses.find_active_by_account_and_category(
            tenant, account, first.category
        )
        assert active is not None and active.hypothesis_id == first.hypothesis_id
        outreach = await uow.hypotheses.list_for_outreach(tenant, None, 10)
        assert [item.hypothesis_id for item in outreach] == [first.hypothesis_id]
        ready = await uow.needs.list_sourcing_ready(tenant, 10)
        assert [item.need_id for item in ready] == [ready_old.need_id, ready_new.need_id]
        by_account = await uow.needs.list_by_account(tenant, account)
        assert {item.need_id for item in by_account} == {ready_old.need_id, ready_new.need_id}


async def test_repo_validated_need_all_factual_jsonb_roundtrip(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    need = _need(tenant)
    async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
        await uow.needs.add(need)
        loaded = await uow.needs.get(tenant, need.need_id)
        assert loaded is not None
        for field_name in (
            "product_category",
            "application",
            "material",
            "size_spec",
            "quantity",
            "packaging",
            "destination",
            "required_by",
            "target_price",
            "current_supply_issue",
            "certification_required",
        ):
            assert getattr(loaded, field_name) == getattr(need, field_name), field_name
        assert loaded.source_conversation_id == need.source_conversation_id
        assert loaded.cluster_id is None


async def test_repo_append_field_history_row_shape(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    need = _need(tenant)
    async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
        await uow.needs.add(need)
        await uow.needs.append_field_history(
            tenant,
            need.need_id,
            "quantity",
            None,
            "5000",
            "msg_conv_010",
            "emp-1",
        )
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = (
            await session.execute(
                select(tables.ValidatedNeedFieldHistoryRow).where(
                    tables.ValidatedNeedFieldHistoryRow.tenant_id == str(tenant)
                )
            )
        ).scalar_one()
    assert row.need_id == str(need.need_id)
    assert row.field_name == "quantity"
    assert row.old_value is None
    assert row.new_value == "5000"
    assert row.source_message_id == "msg_conv_010"
    assert row.changed_by == "emp-1"
    assert row.changed_at == NOW


async def test_repo_get_for_update_locks_row(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    need = _need(tenant)
    async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
        await uow.needs.add(need)
    a_locked = asyncio.Event()
    release_a = asyncio.Event()
    b_locked = asyncio.Event()

    async def run_a() -> None:
        async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
            assert await uow.needs.get_for_update(tenant, need.need_id) is not None
            a_locked.set()
            await release_a.wait()

    async def run_b() -> None:
        await a_locked.wait()
        async with _uow_type()(factory, tenant, now=lambda: NOW) as uow:
            assert await uow.needs.get_for_update(tenant, need.need_id) is not None
            b_locked.set()

    task_a = asyncio.create_task(run_a())
    task_b = asyncio.create_task(run_b())
    await asyncio.wait_for(a_locked.wait(), timeout=2)
    await asyncio.sleep(0.1)
    assert not b_locked.is_set()
    release_a.set()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2)
    assert b_locked.is_set()


async def test_repo_cross_tenant_raises_and_audit_has_no_content(
    demand_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    caplog.clear()
    async with _uow_type()(factory, tenant_b, now=lambda: NOW) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.hypotheses.get(
                tenant_a, _models.NeedHypothesisId(new_id("hyp"))
            )
        with pytest.raises(TenantIsolationViolation):
            await uow.needs.get(tenant_a, _models.ValidatedNeedId(new_id("need")))
    critical = [record for record in caplog.records if record.levelname == "CRITICAL"]
    assert len(critical) == 2
    assert {getattr(record, "action", None) for record in critical} == {
        "need_hypothesis_get",
        "validated_need_get",
    }
    for record in critical:
        assert record.message == "检测到跨租户数据隔离违规"
        assert getattr(record, "tenant_id", None) == str(tenant_b)


async def test_create_hypothesis_roundtrip_persists_evidence_snapshot(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    signal_id = await _signal(service, tenant)

    hypothesis_id = await service.create_hypothesis(
        tenant,
        _models.ProspectAccountId(new_id("acc")),
        "stainless steel hinges",
        [signal_id],
        "Acme 新增产品线，可能需要耐腐蚀五金",
        "model-v1",
    )
    assert hypothesis_id.startswith("hyp_")
    rows = await _hypothesis_rows(factory, tenant)
    assert len(rows) == 1
    row = rows[0]
    assert row.hypothesis_id == hypothesis_id
    assert row.category == "stainless steel hinges"
    assert row.status == "inferred"
    assert row.signal_ids == [signal_id]
    assert row.reasoning["inferred_by"] == "model-v1"
    assert row.reasoning["based_on"][0]["source_id"] == "msg_conv_001"
    events = await _outbox_events(factory, tenant)
    assert len(events) == 2
    created = [event for event in events if event.event_type == "NeedHypothesisCreated"]
    assert len(created) == 1
    payload = dict(created[0].event_payload)
    assert set(payload) <= {
        "tenant_id",
        "occurred_at",
        "run_id",
        "hypothesis_id",
        "account_id",
        "category",
        "confidence_tier",
    }
    assert OBSERVATION_MARKER not in str(payload)


async def test_create_hypothesis_merges_into_active_hypothesis(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    account = _models.ProspectAccountId(new_id("acc"))
    first = await _signal(service, tenant)
    second = await _signal(service, tenant, source_id="msg_conv_002")

    first_id = await service.create_hypothesis(
        tenant, account, "stainless steel hinges", [first], "推断一", "model-v1"
    )
    merged_id = await service.create_hypothesis(
        tenant, account, "stainless steel hinges", [second], "推断一", "model-v1"
    )
    assert merged_id == first_id
    rows = await _hypothesis_rows(factory, tenant)
    assert len(rows) == 1
    assert sorted(rows[0].signal_ids) == sorted([first, second])
    sources = {item["source_id"] for item in rows[0].reasoning["based_on"]}
    assert sources == {"msg_conv_001", "msg_conv_002"}
    assert len(await _outbox_events(factory, tenant)) == 3


async def test_create_hypothesis_merge_same_source_keeps_highest_evidence(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    account = _models.ProspectAccountId(new_id("acc"))
    public_signal = await _signal(
        service,
        tenant,
        signal_type="facility_expansion",
        source_id="msg_shared",
    )
    direct_signal = await _signal(
        service,
        tenant,
        signal_type="inbound_inquiry",
        source_id="msg_shared",
    )
    await service.create_hypothesis(
        tenant, account, "hinges", [public_signal], "推断", "model-v1"
    )
    await service.create_hypothesis(
        tenant, account, "hinges", [direct_signal], "推断", "model-v1"
    )
    rows = await _hypothesis_rows(factory, tenant)
    evidence = rows[0].reasoning["based_on"]
    assert len(evidence) == 1
    assert evidence[0]["level"] == "customer_interest_reply"


async def test_create_hypothesis_concurrent_same_key_exactly_one_row_one_event(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)
    signal_id = await _signal(service_a, tenant)
    account = _models.ProspectAccountId(new_id("acc"))

    results = await asyncio.gather(
        service_a.create_hypothesis(
            tenant, account, "stainless steel hinges", [signal_id], "推断", "model-v1"
        ),
        service_b.create_hypothesis(
            tenant, account, "stainless steel hinges", [signal_id], "推断", "model-v1"
        ),
        return_exceptions=True,
    )
    assert all(isinstance(result, str) for result in results), results
    assert results[0] == results[1]
    assert len(await _hypothesis_rows(factory, tenant)) == 1
    assert len(await _outbox_events(factory, tenant)) == 2


async def test_create_hypothesis_same_category_different_accounts_are_distinct(
    demand_db: AsyncEngine,
) -> None:
    """活跃唯一身份必须包含 account_id，不能把不同企业误并。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    first_signal = await _signal(service, tenant)
    second_signal = await _signal(service, tenant, source_id="msg_conv_002")
    first_id = await service.create_hypothesis(
        tenant,
        _models.ProspectAccountId(new_id("acc")),
        "stainless steel hinges",
        [first_signal],
        "推断",
        "model-v1",
    )
    second_id = await service.create_hypothesis(
        tenant,
        _models.ProspectAccountId(new_id("acc")),
        "stainless steel hinges",
        [second_signal],
        "推断",
        "model-v1",
    )
    assert first_id != second_id
    assert len(await _hypothesis_rows(factory, tenant)) == 2
    assert len(await _outbox_events(factory, tenant)) == 4


async def test_create_hypothesis_rejects_bad_or_invisible_signals(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    other_service = _service(factory, other_tenant, clock)
    account = _models.ProspectAccountId(new_id("acc"))
    with pytest.raises(ValidationError, match="需求信号不能为空"):
        await service.create_hypothesis(
            tenant, account, "cat", [], "推断", "model-v1"
        )
    with pytest.raises(ValidationError, match="需求信号不存在"):
        await service.create_hypothesis(
            tenant, account, "cat", [new_id("sig")], "推断", "model-v1"
        )
    invisible = await _signal(other_service, other_tenant, source_id="msg_other")
    with pytest.raises(ValidationError, match="需求信号不存在"):
        await service.create_hypothesis(
            tenant, account, "cat", [invisible], "推断", "model-v1"
        )
    signal_id = await _signal(service, tenant)
    await service.discard_signal(tenant, signal_id, "noise")
    with pytest.raises(ValidationError, match="需求信号已丢弃"):
        await service.create_hypothesis(
            tenant, account, "cat", [signal_id], "推断", "model-v1"
        )
    assert await _hypothesis_rows(factory, tenant) == []
