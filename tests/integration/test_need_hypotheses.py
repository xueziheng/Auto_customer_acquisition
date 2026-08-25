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

from domains.demand.errors import (
    HypothesisAlreadyResolvedError,
    InsufficientEvidenceError,
    SourcingThresholdNotMetError,
)
from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    MessageId,
    OutboundMessageId,
    TenantId,
    new_id,
)
from shared.schemas.money import Money
from shared.schemas.provenance import SourceType

_models = importlib.import_module("domains.demand.models")

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
OBSERVATION_MARKER = "Acme SECRET-OBSERVATION-77 opened a new plant in Rotterdam."
WEB_SNAPSHOT_ARTIFACT_REF = "art_01K3H0T8NBWM3KGT9XQ06YRC5V"
WEB_PAGE_HASH = "e" * 64


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
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    clock: MutableClock,
    *,
    account_names: object | None = None,
    customer_evidence: object | None = None,
) -> DemandService:
    impl_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    kwargs: dict[str, object] = {
        "now": clock.now,
        "account_names": account_names,
    }
    if customer_evidence is not None:
        kwargs["customer_evidence"] = customer_evidence
    return impl_type(
        lambda requested: _uow_type()(factory, requested, now=clock.now),
        **kwargs,
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


async def _seed_web_snapshot(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> None:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        session.add(
            tables.RawArtifactRow(
                tenant_id=str(tenant),
                artifact_id=WEB_SNAPSHOT_ARTIFACT_REF,
                kind="web_snapshot",
                content_hash=WEB_PAGE_HASH,
                size_bytes=20,
                mime_type="text/html",
                object_key=f"raw/{tenant}/{WEB_SNAPSHOT_ARTIFACT_REF}",
                uploaded_by=None,
                uploaded_at=NOW,
            )
        )
        await session.commit()


class _AccountOrganizationFacts:
    def __init__(
        self, tenant_id: TenantId, account_id: object, name: str, country: str
    ) -> None:
        self._tenant_id = tenant_id
        self._account_id = account_id
        self._name = name
        self._country = country

    async def names_for(self, tenant_id, account_ids):
        assert tenant_id == self._tenant_id
        assert account_ids == (self._account_id,)
        return {self._account_id: self._name}

    async def countries_for(self, tenant_id, account_ids):
        assert tenant_id == self._tenant_id
        assert account_ids == (self._account_id,)
        return {self._account_id: self._country}

    async def domains_for(self, tenant_id, account_ids):
        assert tenant_id == self._tenant_id
        assert account_ids == (self._account_id,)
        return {self._account_id: "apple.com"}


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


async def test_discovery_view_exposes_only_typed_organization_and_opaque_evidence(
    demand_db: AsyncEngine,
) -> None:
    """账户发现视图不暴露可能含联系人姓名的推断、摘要或 URL path。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account_id = _models.ProspectAccountId(new_id("acc"))
    clock = MutableClock(NOW)
    service = _service(
        factory,
        tenant,
        clock,
        account_names=_AccountOrganizationFacts(tenant, account_id, "Apple", "US"),
    )
    await _seed_web_snapshot(factory, tenant)
    signal_id = await _signal(
        service,
        tenant,
        raw_observation="alice and ALICE SMITH reported a factory expansion",
        source_type="web_page",
        source_id=WEB_PAGE_HASH,
        source_url="https://example.com/people/Alice-SMITH",
        page_hash=WEB_PAGE_HASH,
        snapshot_artifact_ref=WEB_SNAPSHOT_ARTIFACT_REF,
    )
    hypothesis_id = await service.create_hypothesis(
        tenant,
        account_id,
        "五金",
        [signal_id],
        "Alice Smith 可能需要铰链",
        "model-v1",
    )

    view = await service.get_hypothesis_for_discovery(tenant, hypothesis_id)

    assert view.__dict__ == {
        "hypothesis_id": str(hypothesis_id),
        "account_id": str(account_id),
        "organization_name": "Apple",
        "country": "US",
        "website_domain": "apple.com",
        "category": "五金",
        "source_signal_refs": (signal_id,),
    }
    serialized = repr(view)
    assert "Alice" not in serialized
    assert "alice" not in serialized
    assert "/people/" not in serialized


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


async def _create_promotable_hypothesis(
    service: DemandService,
    tenant: TenantId,
    category: str = "stainless steel hinges",
) -> str:
    """创建带客户直接兴趣证据、可晋升的需求假设。"""
    signal_id = await _signal(service, tenant)
    return await service.create_hypothesis(
        tenant,
        _models.ProspectAccountId(new_id("acc")),
        category,
        [signal_id],
        "入站询盘表明可能需要五金件",
        "model-v1",
    )


async def test_promote_rejects_agent_inference_evidence(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    await _seed_web_snapshot(factory, tenant)
    signal_id = await _signal(
        service,
        tenant,
        signal_type="product_line_expansion",
        source_type="web_page",
        source_id=WEB_PAGE_HASH,
        source_url="https://example.com/acme",
        page_hash=WEB_PAGE_HASH,
        snapshot_artifact_ref=WEB_SNAPSHOT_ARTIFACT_REF,
    )
    hypothesis_id = await service.create_hypothesis(
        tenant,
        _models.ProspectAccountId(new_id("acc")),
        "hinges",
        [signal_id],
        "工厂扩建，可能需要五金",
        "model-v1",
    )
    before = len(await _outbox_events(factory, tenant))

    with pytest.raises(InsufficientEvidenceError):
        await service.promote_to_validated(
            tenant,
            hypothesis_id,
            "msg_conv_001",
            {"product_category": "hinges"},
            "emp-1",
        )

    rows = await _hypothesis_rows(factory, tenant)
    assert rows[0].status == "inferred"
    assert len(await _outbox_events(factory, tenant)) == before


async def test_customer_reply_evidence_requires_verified_durable_binding(
    demand_db: AsyncEngine,
) -> None:
    """删除 verifier 或只传 synthetic MessageId 时，客户证据门槛必须仍关闭。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account_id = _models.ProspectAccountId(new_id("acc"))
    clock = MutableClock(NOW)
    unverified = _service(factory, tenant, clock)
    await _seed_web_snapshot(factory, tenant)
    signal_id = await _signal(
        unverified,
        tenant,
        signal_type="product_line_expansion",
        source_type="web_page",
        source_id=WEB_PAGE_HASH,
        source_url="https://example.com/acme",
        page_hash=WEB_PAGE_HASH,
        snapshot_artifact_ref=WEB_SNAPSHOT_ARTIFACT_REF,
    )
    hypothesis_id = await unverified.create_hypothesis(
        tenant,
        account_id,
        "hinges",
        [signal_id],
        "工厂扩建，可能需要五金",
        "model-v1",
    )
    message_id = MessageId(new_id("msg"))
    schemas = importlib.import_module("domains.demand.schemas")
    claim = schemas.CustomerReplyEvidenceClaim(
        hypothesis_id=hypothesis_id,
        source_message_id=message_id,
        outbound_message_id=OutboundMessageId(
            f"<reply-demand.{'e' * 64}@messages.tradeos.invalid>"
        ),
        enrollment_id=EnrollmentId(new_id("enr")),
        account_id=account_id,
        contact_point_id=ContactPointId(new_id("cp")),
    )

    with pytest.raises(ValidationError, match="未验证"):
        await unverified.record_customer_reply_evidence(tenant, claim)
    with pytest.raises(InsufficientEvidenceError):
        await unverified.promote_to_validated(
            tenant,
            hypothesis_id,
            message_id,
            {"product_category": "hinges"},
        )

    class Verifier:
        def __init__(self, proof: object) -> None:
            self.proof = proof

        async def verify(self, requested_tenant, claim):
            assert requested_tenant == tenant
            assert claim.hypothesis_id == hypothesis_id
            assert claim.source_message_id == message_id
            return self.proof

    proof = schemas.VerifiedCustomerReplyEvidence(
        tenant_id=tenant,
        hypothesis_id=hypothesis_id,
        source_message_id=message_id,
        account_id=account_id,
        evidence_level=EvidenceLevel.CUSTOMER_SPECIFICATION,
        classified_by="reply-model-v4",
        classified_at=NOW,
    )
    verified = _service(
        factory,
        tenant,
        clock,
        customer_evidence=Verifier(proof),
    )
    await verified.record_customer_reply_evidence(tenant, claim)

    confidence = await verified.get_confidence(tenant, hypothesis_id)
    assert confidence.tier.value == "high"


async def test_promote_success_status_and_event(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)

    need_id = await service.promote_to_validated(
        tenant,
        hypothesis_id,
        "msg_conv_001",
        {"product_category": "hinges"},
        "emp-1",
    )
    assert need_id.startswith("need_")
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        need_rows = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    assert len(need_rows) == 1
    row = need_rows[0]
    assert row.status == "validated"
    assert row.product_category["value"] == "hinges"
    assert row.product_category["provenance"]["source_id"] == "msg_conv_001"
    assert row.product_category["provenance"]["confirmed_by"] == "emp-1"
    hyp_rows = await _hypothesis_rows(factory, tenant)
    assert hyp_rows[0].status == "validated"
    assert hyp_rows[0].validated_need_id == need_id
    events = await _outbox_events(factory, tenant)
    validated = [event for event in events if event.event_type == "NeedValidated"]
    assert len(validated) == 1
    payload = dict(validated[0].event_payload)
    assert set(payload) <= {
        "tenant_id",
        "occurred_at",
        "run_id",
        "need_id",
        "account_id",
        "category",
        "evidence_level",
        "completeness",
    }
    assert payload["completeness"] == 1
    assert payload["evidence_level"] == "customer_interest_reply"
    assert OBSERVATION_MARKER not in str(payload)

    hypothesis2 = await _create_promotable_hypothesis(
        service, tenant, category="aluminum profiles"
    )
    need2 = await service.promote_to_validated(
        tenant,
        hypothesis2,
        "msg_conv_002",
        {
            "product_category": "aluminum profiles",
            "application": "marine use",
            "quantity": 5000,
        },
        None,
    )
    async with factory() as session:
        row2 = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need2)
                )
            )
        ).scalar_one()
    assert row2.status == "sourcing_ready"
    assert row2.quantity["value"] == 5000

    with pytest.raises(ValidationError, match="产品类别不能为空"):
        await service.promote_to_validated(
            tenant,
            hypothesis2,
            "msg_conv_003",
            {"quantity": 5000},
            None,
        )


async def test_promote_idempotent_and_concurrent(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    fields = {"product_category": "hinges", "quantity": 3000}
    need_id = await service.promote_to_validated(
        tenant, hypothesis_id, "msg_conv_001", fields, None
    )
    again = await service.promote_to_validated(
        tenant, hypothesis_id, "msg_conv_001", fields, None
    )
    assert again == need_id
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        count = len(
            (
                await session.execute(
                    select(tables.ValidatedNeedRow).where(
                        tables.ValidatedNeedRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
        )
    assert count == 1
    validated = [
        event
        for event in await _outbox_events(factory, tenant)
        if event.event_type == "NeedValidated"
    ]
    assert len(validated) == 1

    hypothesis2 = await _create_promotable_hypothesis(
        service, tenant, category="hinges bulk"
    )
    service_b = _service(factory, tenant, clock)
    results = await asyncio.gather(
        service.promote_to_validated(
            tenant, hypothesis2, "msg_conv_010", fields, None
        ),
        service_b.promote_to_validated(
            tenant, hypothesis2, "msg_conv_010", fields, None
        ),
        return_exceptions=True,
    )
    assert all(isinstance(result, str) for result in results), results
    assert results[0] == results[1]
    validated = [
        event
        for event in await _outbox_events(factory, tenant)
        if event.event_type == "NeedValidated"
    ]
    assert len(validated) == 2


async def test_reject_semantics(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    await service.reject_hypothesis(tenant, hypothesis_id, "no_budget", "emp-1")
    await service.reject_hypothesis(tenant, hypothesis_id, "no_budget", "emp-1")
    rows = await _hypothesis_rows(factory, tenant)
    assert rows[0].status == "rejected"
    assert rows[0].rejection_reason == "no_budget"

    from shared.errors import InvalidStateTransition

    with pytest.raises(InvalidStateTransition) as exc_info:
        await service.reject_hypothesis(tenant, hypothesis_id, "no_reply", "emp-1")
    assert str(exc_info.value) == "拒绝原因冲突，拒绝覆盖"
    rows = await _hypothesis_rows(factory, tenant)
    assert rows[0].rejection_reason == "no_budget"
    rejected = [
        event
        for event in await _outbox_events(factory, tenant)
        if event.event_type == "NeedHypothesisRejected"
    ]
    assert len(rejected) == 1
    assert dict(rejected[0].event_payload)["reason"] == "no_budget"

    validated_id = await _create_promotable_hypothesis(
        service, tenant, "validated target"
    )
    await service.promote_to_validated(
        tenant,
        validated_id,
        "msg_conv_020",
        {"product_category": "hinges"},
        None,
    )
    with pytest.raises(HypothesisAlreadyResolvedError):
        await service.reject_hypothesis(
            tenant, validated_id, "no_budget", "emp-1"
        )


async def _promote_basic_need(
    service: DemandService,
    tenant: TenantId,
    category: str = "hinges",
) -> str:
    """创建并晋升一个完整度为 1 的基础需求。"""
    hypothesis_id = await _create_promotable_hypothesis(service, tenant, category)
    return await service.promote_to_validated(
        tenant,
        hypothesis_id,
        "msg_conv_001",
        {"product_category": category},
        None,
    )


async def test_update_need_fields_history_and_auto_advance(
    demand_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    need_id = await _promote_basic_need(service, tenant)
    tables = importlib.import_module("infra.db.tables")

    await service.update_need_fields(
        tenant,
        need_id,
        {"application": "marine use", "quantity": 5000},
        "msg_conv_010",
        "emp-1",
    )
    async with factory() as session:
        history = (
            await session.execute(
                select(tables.ValidatedNeedFieldHistoryRow).where(
                    tables.ValidatedNeedFieldHistoryRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        need_row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert len(history) == 2
    entry = next(item for item in history if item.field_name == "quantity")
    assert entry.old_value is None
    assert entry.new_value == "5000"
    assert entry.source_message_id == "msg_conv_010"
    assert entry.changed_by == "emp-1"
    assert need_row.status == "sourcing_ready"
    assert need_row.quantity["value"] == 5000
    validated_events = [
        event
        for event in await _outbox_events(factory, tenant)
        if event.event_type == "NeedValidated"
    ]
    assert len(validated_events) == 1

    await service.update_need_fields(
        tenant,
        need_id,
        {"destination": "Rotterdam"},
        "msg_conv_011",
        "emp-1",
    )
    async with factory() as session:
        need_row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert need_row.status == "sourcing_ready"

    with pytest.raises(ValidationError, match="未知需求字段"):
        await service.update_need_fields(
            tenant,
            need_id,
            {"product_category": "other"},
            "msg_conv_012",
            "emp-1",
        )
    with pytest.raises(ValidationError, match="未知需求字段"):
        await service.update_need_fields(
            tenant,
            need_id,
            {"bogus_field": "x"},
            "msg_conv_012",
            "emp-1",
        )
    with pytest.raises(ValidationError, match="需求字段类型无效"):
        await service.update_need_fields(
            tenant,
            need_id,
            {"quantity": "not-an-int"},
            "msg_conv_012",
            "emp-1",
        )

    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        frozen = await uow.needs.get_for_update(
            tenant, _models.ValidatedNeedId(need_id)
        )
        assert frozen is not None
        await uow.needs.update(
            _models.ValidatedNeed(
                **{**frozen.__dict__, "status": _models.NeedStatus.FULFILLED}
            )
        )

    from shared.errors import InvalidStateTransition

    with pytest.raises(InvalidStateTransition, match="需求已终结"):
        await service.update_need_fields(
            tenant,
            need_id,
            {"quantity": 1},
            "msg_conv_013",
            "emp-1",
        )


async def test_reply_extracted_field_keeps_message_provenance_and_verbatim_quote(
    demand_db: AsyncEngine,
) -> None:
    """回复动作写入事实时，值与客户逐字原话必须一起耐久化并可查询。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _service(factory, tenant, MutableClock(NOW))
    need_id = await _promote_basic_need(service, tenant)
    quote = "We need 5000 stainless steel hinges."

    await service.update_need_fields(
        tenant,
        need_id,
        {
            "quantity": {
                "value": "5000",
                "quote": quote,
                "extracted_by": "reply-model-v3",
            }
        },
        "msg_reply_specification_1",
        None,
    )
    await service.update_need_fields(
        tenant,
        need_id,
        {
            "quantity": {
                "value": "5000",
                "quote": quote,
                "extracted_by": "reply-model-v3",
            }
        },
        "msg_reply_specification_1",
        None,
    )

    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = await session.get(tables.ValidatedNeedRow, (str(tenant), str(need_id)))
        history = (
            await session.execute(
                select(tables.ValidatedNeedFieldHistoryRow).where(
                    tables.ValidatedNeedFieldHistoryRow.tenant_id == str(tenant),
                    tables.ValidatedNeedFieldHistoryRow.need_id == str(need_id),
                    tables.ValidatedNeedFieldHistoryRow.field_name == "quantity",
                )
            )
        ).scalars().all()
    assert len(history) == 1
    assert row.quantity["provenance"]["extracted_by"] == "reply-model-v3"
    account_id = _models.ProspectAccountId(row.account_id)
    query_service = _service(
        factory,
        tenant,
        MutableClock(NOW),
        account_names=_AccountOrganizationFacts(tenant, account_id, "Acme", "US"),
    )
    view = await query_service.get_need(tenant, need_id)
    quantity = next(field for field in view.fields if field.name == "quantity")
    assert quantity.value == "5000"
    assert quantity.source_ref == "msg_reply_specification_1"
    assert quantity.source_quote == quote


async def test_mark_sourcing_ready_gates(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    need_id = await _promote_basic_need(service, tenant)
    with pytest.raises(SourcingThresholdNotMetError) as exc_info:
        await service.mark_sourcing_ready(tenant, need_id)
    assert "application" in str(exc_info.value)
    assert "size_spec" in str(exc_info.value)

    await service.update_need_fields(
        tenant,
        need_id,
        {"application": "marine use", "quantity": 200},
        "msg_conv_020",
        None,
    )
    await service.mark_sourcing_ready(tenant, need_id)
    await service.mark_sourcing_ready(tenant, need_id)
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert row.status == "sourcing_ready"

    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        back = await uow.needs.get_for_update(
            tenant, _models.ValidatedNeedId(need_id)
        )
        assert back is not None
        await uow.needs.update(
            _models.ValidatedNeed(
                **{**back.__dict__, "status": _models.NeedStatus.VALIDATED}
            )
        )
    await service.mark_sourcing_ready(tenant, need_id)
    async with factory() as session:
        row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert row.status == "sourcing_ready"


async def test_get_confidence_derived_live(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    result = await service.get_confidence(tenant, hypothesis_id)
    assert result.tier.value in {"mid_high", "high", "very_high", "extreme"}
    assert "base_from_highest" in result.applied_rules
    assert result.explanation
    with pytest.raises(ValidationError, match="需求假设不存在"):
        await service.get_confidence(tenant, new_id("hyp"))


async def test_outbox_and_logs_no_marker_leak(
    demand_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    field_marker = "PRIVATE-FIELD-MARKER-93"
    caplog.clear()

    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    await service.promote_to_validated(
        tenant,
        hypothesis_id,
        "msg_conv_001",
        {
            "product_category": "hinges",
            "quantity": 100,
            "current_supply_issue": field_marker,
        },
        "emp-1",
    )
    rejected_id = await _create_promotable_hypothesis(
        service, tenant, "rejected category"
    )
    await service.reject_hypothesis(tenant, rejected_id, "no_budget", "emp-1")

    events = await _outbox_events(factory, tenant)
    assert len(events) >= 5
    allowed = {
        "tenant_id",
        "occurred_at",
        "run_id",
        "signal_id",
        "entity_name",
        "signal_type",
        "hypothesis_id",
        "account_id",
        "category",
        "confidence_tier",
        "reason",
        "need_id",
        "evidence_level",
        "completeness",
    }
    for event in events:
        assert set(event.event_payload) <= allowed, event.event_type
        blob = str(event.event_payload)
        assert OBSERVATION_MARKER not in blob
        assert field_marker not in blob
    rejected = [
        event
        for event in events
        if event.event_type == "NeedHypothesisRejected"
    ]
    assert [event.event_payload["reason"] for event in rejected] == ["no_budget"]
    assert caplog.records == []


async def test_bus_failure_rolls_back_whole_uow(demand_db: AsyncEngine) -> None:
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    uow_type = _uow_type()
    impl_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    service = _service(factory, tenant, clock)

    class _FailingBus:
        async def publish(self, event: object) -> None:
            raise RuntimeError("bus down")

        async def publish_many(self, events: list[object]) -> None:
            raise RuntimeError("bus down")

    class _BusFailureUoW:
        def __init__(self) -> None:
            self._inner = uow_type(factory, tenant, now=clock.now)

        async def __aenter__(self):
            await self._inner.__aenter__()
            self._inner.bus = _FailingBus()  # type: ignore[assignment]
            return self._inner

        async def __aexit__(self, *args: object):
            return await self._inner.__aexit__(*args)

    failing = impl_type(
        lambda _tenant: _BusFailureUoW(),  # type: ignore[arg-type]
        now=clock.now,
    )
    signal_id = await _signal(service, tenant)
    account_id = _models.ProspectAccountId(new_id("acc"))
    with pytest.raises(RuntimeError, match="bus down"):
        await failing.create_hypothesis(
            tenant,
            account_id,
            "hinges",
            [signal_id],
            "推断",
            "model-v1",
        )
    assert await _hypothesis_rows(factory, tenant) == []
    events = await _outbox_events(factory, tenant)
    assert [event.event_type for event in events] == ["DemandSignalCaptured"]

    hypothesis_id = await service.create_hypothesis(
        tenant,
        account_id,
        "hinges",
        [signal_id],
        "推断",
        "model-v1",
    )
    before = [event.event_type for event in await _outbox_events(factory, tenant)]
    with pytest.raises(RuntimeError, match="bus down"):
        await failing.promote_to_validated(
            tenant,
            hypothesis_id,
            "msg_conv_020",
            {"product_category": "hinges"},
            None,
        )
    rows = await _hypothesis_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].status == "inferred"
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        need_count = len(
            (
                await session.execute(
                    select(tables.ValidatedNeedRow).where(
                        tables.ValidatedNeedRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
        )
    assert need_count == 0
    assert [
        event.event_type for event in await _outbox_events(factory, tenant)
    ] == before
