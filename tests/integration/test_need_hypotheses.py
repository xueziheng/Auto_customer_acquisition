"""NeedHypothesis 与 ValidatedNeed 仓储集成测试（真实 PostgreSQL）。"""

from __future__ import annotations

import asyncio
import importlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.money import Money
from shared.schemas.provenance import SourceType

_models = importlib.import_module("domains.demand.models")

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def demand_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _uow_type():
    return importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork


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
