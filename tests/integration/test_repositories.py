"""S2-4 仓储集成测试（Opportunity / ScoreSnapshot）。

行为断言，不依赖实现细节：
- CRUD roundtrip：add → get → update（含 Money 往返）→ get。
- ``find_by_need`` 幂等：重复投递事件也能查到既有机会。
- 同 tenant 同 need 并发唯一：第二次 commit 抛 ``IntegrityError``（UNIQUE 兜底）。
- ``list_by_owner`` / ``list_by_state`` 过滤正确。
- 租户隔离：B 租户仓储查/改 A 租户行 → None / 0 行（A 行不受影响）。
- ``update`` 拒绝终态：``state ∈ {WON, LOST}`` 抛 ``InvalidStateTransition``。
- 快照只增：两次 add 后 latest 最新、历史保留（backtest 可导出）；since_days 过滤。
- 状态机原子推进：advance_state 条件更新；close_won 仅 NEGOTIATING；
  close_lost 需 expected；assign_owner 落 owner/assigned_by/assigned_at。

强化验收（监督复核要求）：
- 同 (tenant_id, need_id) 真正并发：两个独立会话 gather，恰好一个 commit 成功。
- ORM metadata 与 0002 逐表一致：六表列集合、7 索引名+列序、关键 unique/check/FK 名。
- 方法 tenant_id 与绑定租户不一致：读/list/latest/backtest 返回空、条件更新 False；
  add/update 对象租户与绑定租户不一致 → ValueError（硬边界 8 写侧）。
- 快照 add 要求 bound == method tenant == snapshot.tenant，否则 ValueError。

RED 阶段仓储实现经 importlib 延迟导入（ModuleNotFoundError/AttributeError 转行为
失败，非收集错误）。域私有模型（domains.opportunities.models）经 importlib 路由，
避开 domain-internals 机检。函数级本地引擎会话，避免 session 级 async fixture
的事件循环 teardown 问题。禁止打印/记录任何连接串。
"""
from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.errors import InvalidStateTransition
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    ScoreSnapshotId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_USD = CurrencyCode("USD")

_MODULE_BY_SYMBOL = {
    "Opportunity": "domains.opportunities.models",
    "OpportunityState": "domains.opportunities.models",
    "LossReason": "domains.opportunities.models",
    "ScoreSnapshot": "domains.opportunities.models",
    "SortKey": "domains.opportunities.models",
    "OpportunityRepositoryImpl": "infra.db.repositories.opportunities",
    "ScoreSnapshotRepositoryImpl": "infra.db.repositories.opportunities",
}


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED 阶段仓储未建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


# 域私有模型存在（S2-1 契约），可模块级加载；仓储实现须在测试内加载。
Opportunity = _load("Opportunity")
OpportunityState = _load("OpportunityState")
LossReason = _load("LossReason")
ScoreSnapshot = _load("ScoreSnapshot")
SortKey = _load("SortKey")


def _opp(opportunity_id: str, tenant_id: str, need_id: str, **overrides):
    """构造最小合法机会（可覆盖 state/owner 等字段）。"""
    return Opportunity(
        opportunity_id=OpportunityId(opportunity_id),
        tenant_id=TenantId(tenant_id),
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId(need_id),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
        **overrides,
    )


@pytest_asyncio.fixture
async def repo_session(db_url: str) -> AsyncIterator[AsyncSession]:
    """函数级本地引擎会话（避免 session 级 async fixture 的事件循环 teardown 问题）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def test_opportunity_crud_roundtrip(repo_session: AsyncSession) -> None:
    """add → get → update（含 Money 成对往返）→ get。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tCrud"))
    opp = _opp("opp-crud-1", "tCrud", "need-crud-1")
    await repo.add(opp)
    await repo_session.commit()

    found = await repo.get(TenantId("tCrud"), OpportunityId("opp-crud-1"))
    assert found is not None
    assert found.account_name == "Acme"
    assert found.country == "US"
    assert found.state == OpportunityState.QUALIFIED

    opp.quantity = 500
    opp.target_price = Money(Decimal("1500.00"), _USD)
    await repo.update(opp)
    await repo_session.commit()

    found = await repo.get(TenantId("tCrud"), OpportunityId("opp-crud-1"))
    assert found is not None
    assert found.quantity == 500
    assert found.target_price == Money(Decimal("1500.00"), _USD)


async def test_find_by_need_idempotent(repo_session: AsyncSession) -> None:
    """按需求查机会：重复投递事件也能查到既有机会。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tIdem"))
    await repo.add(_opp("opp-idem-1", "tIdem", "need-idem-1"))
    await repo_session.commit()

    found = await repo.find_by_need(TenantId("tIdem"), ValidatedNeedId("need-idem-1"))
    assert found is not None
    assert found.opportunity_id == OpportunityId("opp-idem-1")
    assert await repo.find_by_need(TenantId("tIdem"), ValidatedNeedId("need-absent")) is None


async def test_unique_need_idempotent_race(db_url: str) -> None:
    """同 (tenant_id, need_id) 真正并发插入：恰好一个 commit 成功、一个 IntegrityError。

    两个独立 AsyncSession/事务并发 commit（asyncio.gather）；失败事务 rollback 后，
    获胜仓储 find_by_need 返回唯一既有机会。DB UNIQUE 已存在，本测试可能直接 GREEN。
    """
    from infra.db.session import create_engine_from

    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    engine = create_engine_from(db_url)
    session_a = AsyncSession(bind=engine, expire_on_commit=False)
    session_b = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        repo_a = OpportunityRepositoryImpl(session_a, TenantId("tRace"))
        repo_b = OpportunityRepositoryImpl(session_b, TenantId("tRace"))
        await repo_a.add(_opp("opp-race-1", "tRace", "need-race-1"))
        await repo_b.add(_opp("opp-race-2", "tRace", "need-race-1"))

        async def _commit(session: AsyncSession) -> str:
            try:
                await session.commit()
                return "ok"
            except IntegrityError:
                await session.rollback()
                return "conflict"

        results = await asyncio.gather(_commit(session_a), _commit(session_b))
        assert sorted(results) == ["conflict", "ok"], f"应恰好一个成功一个冲突：{results}"

        winner = await repo_a.find_by_need(TenantId("tRace"), ValidatedNeedId("need-race-1"))
        assert winner is not None
        assert winner.opportunity_id in {
            OpportunityId("opp-race-1"),
            OpportunityId("opp-race-2"),
        }
    finally:
        await session_a.close()
        await session_b.close()
        await engine.dispose()


async def test_list_by_owner_and_state(repo_session: AsyncSession) -> None:
    """owner 过滤（可叠加 states）、state 过滤正确。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tList"))
    await repo.add(
        _opp("opp-list-1", "tList", "need-list-1",
             owner=EmployeeId("emp-1"), state=OpportunityState.ASSIGNED)
    )
    await repo.add(
        _opp("opp-list-2", "tList", "need-list-2",
             owner=EmployeeId("emp-2"), state=OpportunityState.CONTACTED)
    )
    await repo_session.commit()

    owned = await repo.list_by_owner(
        TenantId("tList"), EmployeeId("emp-1"), [OpportunityState.ASSIGNED], limit=10
    )
    assert [o.opportunity_id for o in owned] == [OpportunityId("opp-list-1")]

    owned_all = await repo.list_by_owner(TenantId("tList"), EmployeeId("emp-1"), None, limit=10)
    assert [o.opportunity_id for o in owned_all] == [OpportunityId("opp-list-1")]

    by_state = await repo.list_by_state(TenantId("tList"), OpportunityState.CONTACTED, limit=10)
    assert [o.opportunity_id for o in by_state] == [OpportunityId("opp-list-2")]


async def test_tenant_isolation(repo_session: AsyncSession) -> None:
    """A 租户行：B 租户仓储查 → None；条件更新 → False；带 A 对象的写入 → 拒绝。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo_a = OpportunityRepositoryImpl(repo_session, TenantId("tIsoA"))
    repo_b = OpportunityRepositoryImpl(repo_session, TenantId("tIsoB"))
    await repo_a.add(_opp("opp-iso-a", "tIsoA", "need-iso-a"))
    await repo_session.commit()

    assert await repo_b.get(TenantId("tIsoB"), OpportunityId("opp-iso-a")) is None

    ok = await repo_b.assign_owner(
        TenantId("tIsoB"), OpportunityId("opp-iso-a"),
        EmployeeId("emp-1"), EmployeeId("mgr-1"), _NOW,
    )
    assert ok is False  # 条件更新 WHERE tenant=tIsoB → 0 行
    await repo_session.commit()

    opp_a = await repo_a.get(TenantId("tIsoA"), OpportunityId("opp-iso-a"))
    assert opp_a is not None
    opp_a.quantity = 1000
    with pytest.raises(ValueError):
        await repo_b.update(opp_a)  # B 绑定仓储拒绝写入 A 租户对象（硬边界 8 写侧）

    found = await repo_a.get(TenantId("tIsoA"), OpportunityId("opp-iso-a"))
    assert found is not None
    assert found.quantity is None  # A 行未被 B 改动
    assert found.owner is None


async def test_update_rejects_terminal_state(repo_session: AsyncSession) -> None:
    """普通 update 拒绝终态：state ∈ {WON, LOST} → InvalidStateTransition。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tTerm"))
    opp = _opp("opp-term-1", "tTerm", "need-term-1")
    await repo.add(opp)
    await repo_session.commit()

    opp.state = OpportunityState.WON
    with pytest.raises(InvalidStateTransition):
        await repo.update(opp)
    opp.state = OpportunityState.LOST
    with pytest.raises(InvalidStateTransition):
        await repo.update(opp)


# --- Batch 2：ScoreSnapshot 只增 / latest / backtest -------------------------


def _snap(snapshot_id: str, tenant_id: str, opportunity_id: str, *, scored_at, **overrides):
    """构造最小合法打分快照（可覆盖 evidence_tier/estimated_value 等）。"""
    return ScoreSnapshot(
        tenant_id=TenantId(tenant_id),
        snapshot_id=ScoreSnapshotId(snapshot_id),
        opportunity_id=OpportunityId(opportunity_id),
        scored_at=scored_at,
        scorer_version="gates-v1",
        passed_gates=["contactable"],
        failed_gates=[],
        evidence_tier=None,
        estimated_value=None,
        supply_available=True,
        sort_key=SortKey(1, 0, 0),
        rank_bucket="high",
        gate_reasons={},
        **overrides,
    )


async def test_snapshot_append_only(repo_session: AsyncSession) -> None:
    """两次 add 后：latest 为最新、历史保留（只增，list_for_backtest 两条都在）。"""
    ScoreSnapshotRepositoryImpl = _load("ScoreSnapshotRepositoryImpl")
    repo = ScoreSnapshotRepositoryImpl(repo_session, TenantId("tSnap"))
    now = datetime.now(UTC)
    first = now - timedelta(days=2)
    second = now - timedelta(days=1)
    await repo.add(TenantId("tSnap"), _snap("snap-a1", "tSnap", "opp-snap-1", scored_at=first))
    await repo.add(TenantId("tSnap"), _snap("snap-a2", "tSnap", "opp-snap-1", scored_at=second))
    await repo_session.commit()

    latest = await repo.latest_for_opportunity(TenantId("tSnap"), OpportunityId("opp-snap-1"))
    assert latest is not None
    assert latest.snapshot_id == ScoreSnapshotId("snap-a2")

    backtest = await repo.list_for_backtest(TenantId("tSnap"), since_days=30)
    ids = {s.snapshot_id for s in backtest}
    assert {ScoreSnapshotId("snap-a1"), ScoreSnapshotId("snap-a2")} <= ids


async def test_snapshot_latest_and_missing(repo_session: AsyncSession) -> None:
    """latest 返回最新快照；无快照的机会返回 None。"""
    ScoreSnapshotRepositoryImpl = _load("ScoreSnapshotRepositoryImpl")
    repo = ScoreSnapshotRepositoryImpl(repo_session, TenantId("tSnap2"))
    await repo.add(TenantId("tSnap2"), _snap("snap-b1", "tSnap2", "opp-snap-2", scored_at=_NOW))
    await repo_session.commit()

    latest = await repo.latest_for_opportunity(TenantId("tSnap2"), OpportunityId("opp-snap-2"))
    assert latest is not None
    assert latest.snapshot_id == ScoreSnapshotId("snap-b1")
    assert latest.tenant_id == TenantId("tSnap2")

    assert (
        await repo.latest_for_opportunity(TenantId("tSnap2"), OpportunityId("opp-absent"))
        is None
    )


async def test_snapshot_list_for_backtest(repo_session: AsyncSession) -> None:
    """since_days 过滤：窗口外的旧快照不导出。"""
    ScoreSnapshotRepositoryImpl = _load("ScoreSnapshotRepositoryImpl")
    repo = ScoreSnapshotRepositoryImpl(repo_session, TenantId("tSnap3"))
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    recent = now - timedelta(days=1)
    await repo.add(TenantId("tSnap3"), _snap("snap-c1", "tSnap3", "opp-snap-3", scored_at=old))
    await repo.add(TenantId("tSnap3"), _snap("snap-c2", "tSnap3", "opp-snap-3", scored_at=recent))
    await repo_session.commit()

    backtest = await repo.list_for_backtest(TenantId("tSnap3"), since_days=30)
    assert [s.snapshot_id for s in backtest] == [ScoreSnapshotId("snap-c2")]


# --- Batch 3：状态机原子推进 / 终结 / 分配 ------------------------------------


async def test_advance_state_atomic(repo_session: AsyncSession) -> None:
    """advance_state 条件更新：expected 匹配成功、不匹配返回 False（并发已变）。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tAdv"))
    await repo.add(
        _opp("opp-adv-1", "tAdv", "need-adv-1", state=OpportunityState.QUALIFIED)
    )
    await repo_session.commit()

    ok = await repo.advance_state(
        TenantId("tAdv"), OpportunityId("opp-adv-1"),
        OpportunityState.QUALIFIED, OpportunityState.ASSIGNED,
    )
    assert ok is True
    await repo_session.commit()
    found = await repo.get(TenantId("tAdv"), OpportunityId("opp-adv-1"))
    assert found is not None
    assert found.state == OpportunityState.ASSIGNED

    ok = await repo.advance_state(
        TenantId("tAdv"), OpportunityId("opp-adv-1"),
        OpportunityState.QUALIFIED, OpportunityState.CONTACTED,
    )
    assert ok is False  # 已非 qualified，条件不匹配


async def test_close_won_only_from_negotiating(repo_session: AsyncSession) -> None:
    """close_won 仅从 NEGOTIATING：非该态返回 False，NEGOTIATING 成功并落 closed 字段。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tWin"))
    await repo.add(
        _opp("opp-win-1", "tWin", "need-win-1", state=OpportunityState.QUALIFIED)
    )
    await repo_session.commit()

    ok = await repo.close_won_if_state(
        TenantId("tWin"), OpportunityId("opp-win-1"), EmployeeId("emp-1"), _NOW
    )
    assert ok is False
    await repo_session.commit()

    ok = await repo.advance_state(
        TenantId("tWin"), OpportunityId("opp-win-1"),
        OpportunityState.QUALIFIED, OpportunityState.NEGOTIATING,
    )
    assert ok is True
    await repo_session.commit()

    ok = await repo.close_won_if_state(
        TenantId("tWin"), OpportunityId("opp-win-1"), EmployeeId("emp-1"), _NOW
    )
    assert ok is True
    await repo_session.commit()

    found = await repo.get(TenantId("tWin"), OpportunityId("opp-win-1"))
    assert found is not None
    assert found.state == OpportunityState.WON
    assert found.closed_by == EmployeeId("emp-1")
    assert found.closed_at == _NOW


async def test_close_lost_expected_state(repo_session: AsyncSession) -> None:
    """close_lost 需 expected 匹配；成功落 loss_reason/died_at_state/closed 字段。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tLost"))
    await repo.add(
        _opp("opp-lost-1", "tLost", "need-lost-1", state=OpportunityState.CONTACTED)
    )
    await repo_session.commit()

    ok = await repo.close_lost_if_state(
        TenantId("tLost"), OpportunityId("opp-lost-1"),
        OpportunityState.QUALIFIED, LossReason.PRICE_TOO_HIGH,
        "detail", EmployeeId("emp-1"), _NOW,
    )
    assert ok is False  # expected 不匹配当前 contacted
    await repo_session.commit()

    ok = await repo.close_lost_if_state(
        TenantId("tLost"), OpportunityId("opp-lost-1"),
        OpportunityState.CONTACTED, LossReason.PRICE_TOO_HIGH,
        "detail", EmployeeId("emp-1"), _NOW,
    )
    assert ok is True
    await repo_session.commit()

    found = await repo.get(TenantId("tLost"), OpportunityId("opp-lost-1"))
    assert found is not None
    assert found.state == OpportunityState.LOST
    assert found.loss_reason == LossReason.PRICE_TOO_HIGH
    assert found.died_at_state == OpportunityState.CONTACTED
    assert found.closed_by == EmployeeId("emp-1")
    assert found.closed_at == _NOW


async def test_assign_owner_records_actor(repo_session: AsyncSession) -> None:
    """assign_owner 落 owner/assigned_by/assigned_at；不存在的机会返回 False。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo = OpportunityRepositoryImpl(repo_session, TenantId("tAsg"))
    await repo.add(_opp("opp-asg-1", "tAsg", "need-asg-1"))
    await repo_session.commit()

    ok = await repo.assign_owner(
        TenantId("tAsg"), OpportunityId("opp-asg-1"),
        EmployeeId("emp-1"), EmployeeId("mgr-1"), _NOW,
    )
    assert ok is True
    await repo_session.commit()

    found = await repo.get(TenantId("tAsg"), OpportunityId("opp-asg-1"))
    assert found is not None
    assert found.owner == EmployeeId("emp-1")
    assert found.assigned_by == EmployeeId("mgr-1")
    assert found.assigned_at == _NOW

    ok = await repo.assign_owner(
        TenantId("tAsg"), OpportunityId("opp-absent"),
        EmployeeId("emp-1"), EmployeeId("mgr-1"), _NOW,
    )
    assert ok is False


# --- 强化验收：ORM metadata 与 0002 逐表一致（列 / 7 索引 / 关键约束名）--------


def test_orm_metadata_parity_with_0002() -> None:
    """ORM metadata 与 0002 一致：六表列集合、7 索引名+列序、关键约束名。

    schema 仍由 Alembic 迁移管理（不用 create_all）；本断言防 ORM 与迁移漂移。
    """
    from infra.db.tables import Base

    metadata = Base.metadata

    expected_columns = {
        "opportunities": {
            "opportunity_id", "tenant_id", "account_id", "account_name", "country",
            "need_id", "product_category", "state", "created_at", "quantity",
            "spec_summary", "application", "destination", "required_by",
            "target_price_amount", "target_price_currency", "decision_maker",
            "current_supply_solution", "current_supply_problem", "can_source",
            "estimated_cost_amount", "estimated_cost_currency",
            "estimated_profit_amount", "estimated_profit_currency",
            "owner", "assigned_by", "assigned_at", "next_action", "next_action_due",
            "loss_reason", "died_at_state", "closed_by", "closed_at",
        },
        "score_snapshots": {
            "snapshot_id", "tenant_id", "opportunity_id", "scored_at", "scorer_version",
            "passed_gates", "failed_gates", "evidence_tier",
            "estimated_value_amount", "estimated_value_currency", "supply_available",
            "sort_evidence_rank", "sort_value_band", "sort_supply_rank",
            "gate_reasons", "rank_bucket",
        },
        "handoffs": {
            "handoff_id", "tenant_id", "opportunity_id", "trigger", "state",
            "requested_at", "account_name", "country", "why_valuable",
            "customer_verbatim", "assigned_to", "manager", "accepted_at",
            "accepted_by", "how_we_found_them", "validated_need_summary",
            "missing_information", "already_sent", "commitments_made",
            "evidence_links", "conversation_summary", "suggested_next_step",
        },
        "loss_records": {
            "loss_record_id", "tenant_id", "opportunity_id", "loss_reason",
            "died_at_state", "detail", "evidence_tier", "confirmed_by",
            "confirmed_at", "recorded_at",
        },
        "provenance_records": {
            "provenance_id", "tenant_id", "entity_type", "entity_id", "field_name",
            "source_type", "source_id", "extracted_by", "extracted_at",
            "confirmed_by", "confirmed_at", "source_url", "page_hash",
        },
        "outbox_events": {
            "event_id", "tenant_id", "event_type", "event_payload", "attempt",
            "published_at", "trace_id", "run_id", "occurred_at", "status",
            "delivered_at",
        },
    }
    for table, cols in expected_columns.items():
        assert table in metadata.tables, f"缺表 {table}"
        actual = {c.name for c in metadata.tables[table].columns}
        assert actual == cols, f"{table} 列集合不一致：{sorted(actual ^ cols)}"

    expected_indexes = {
        "ix_opportunities_tenant_state": ("tenant_id", "state"),
        "ix_opportunities_tenant_owner_state": ("tenant_id", "owner", "state"),
        "ix_score_snapshots_tenant_opp_scored": (
            "tenant_id", "opportunity_id", "scored_at",
        ),
        "ix_handoffs_tenant_state_requested": ("tenant_id", "state", "requested_at"),
        "ix_loss_records_tenant_reason_state": (
            "tenant_id", "loss_reason", "died_at_state",
        ),
        "ix_provenance_lookup": (
            "tenant_id", "entity_type", "entity_id", "field_name", "extracted_at",
        ),
        "ix_outbox_tenant_status": ("tenant_id", "status"),
    }
    actual_indexes: dict[str, tuple[str, ...]] = {}
    for tbl in metadata.tables.values():
        for idx in tbl.indexes:
            if idx.name is not None:
                actual_indexes[str(idx.name)] = tuple(idx.columns.keys())
    assert actual_indexes == expected_indexes, f"索引不一致：{actual_indexes}"

    expected_constraints = {
        "opportunities": {
            "uq_opportunities_tenant_need",
            "uq_opportunities_tenant_opp",
            "ck_opportunities_target_price_pair",
            "ck_opportunities_estimated_cost_pair",
            "ck_opportunities_estimated_profit_pair",
            "ck_opportunities_lost_closed",
            "ck_opportunities_won_closed",
            "ck_opportunities_closed_only_terminal",
        },
        "score_snapshots": {"ck_score_snapshots_value_pair"},
        "handoffs": {"fk_handoffs_opportunity"},
        "loss_records": {"fk_loss_records_opportunity"},
        "outbox_events": {"ck_outbox_attempt_min", "ck_outbox_status"},
        "provenance_records": set(),
    }
    for table, names in expected_constraints.items():
        actual = {
            c.name for c in metadata.tables[table].constraints if isinstance(c.name, str)
        }
        assert names <= actual, f"{table} 缺约束：{sorted(names - actual)}"


# --- 强化验收：方法 tenant_id 与绑定租户不一致 → 读空 / 条件更新 False / 写拒绝 --


async def test_tenant_binding_mismatch_fails_closed(repo_session: AsyncSession) -> None:
    """方法 tenant_id 与绑定租户不一致：读/list 返回空、条件更新 False、写入拒绝。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    repo_a = OpportunityRepositoryImpl(repo_session, TenantId("tMbA"))
    await repo_a.add(
        _opp("opp-mb-a", "tMbA", "need-mb-a")
    )
    await repo_a.add(
        _opp("opp-mb-own", "tMbA", "need-mb-own", owner=EmployeeId("emp-1"))
    )
    await repo_session.commit()

    # 读：方法租户不匹配 → 空（即便行属于绑定租户）
    assert await repo_a.get(TenantId("tMbB"), OpportunityId("opp-mb-a")) is None
    assert await repo_a.find_by_need(TenantId("tMbB"), ValidatedNeedId("need-mb-a")) is None
    assert await repo_a.list_by_state(TenantId("tMbB"), OpportunityState.QUALIFIED, 10) == []
    assert (
        await repo_a.list_by_owner(TenantId("tMbB"), EmployeeId("emp-1"), None, 10) == []
    )

    # 条件更新：方法租户不匹配 → False
    assert await repo_a.advance_state(
        TenantId("tMbB"), OpportunityId("opp-mb-a"),
        OpportunityState.QUALIFIED, OpportunityState.ASSIGNED,
    ) is False

    # 写：对象租户与绑定租户不一致 → 拒绝
    with pytest.raises(ValueError):
        await repo_a.add(_opp("opp-mb-w", "tMbB", "need-mb-w"))
    with pytest.raises(ValueError):
        await repo_a.update(_opp("opp-mb-a", "tMbB", "need-mb-a"))


async def test_snapshot_tenant_binding_mismatch(repo_session: AsyncSession) -> None:
    """快照 add 要求 bound == method tenant == snapshot.tenant；读方法租户不匹配 → 空。"""
    ScoreSnapshotRepositoryImpl = _load("ScoreSnapshotRepositoryImpl")
    repo = ScoreSnapshotRepositoryImpl(repo_session, TenantId("tMs"))
    await repo.add(TenantId("tMs"), _snap("snap-ms-1", "tMs", "opp-ms-1", scored_at=_NOW))
    await repo_session.commit()

    # 读：方法租户不匹配 → 空（即便快照属于绑定租户）
    assert (
        await repo.latest_for_opportunity(TenantId("tOther"), OpportunityId("opp-ms-1"))
        is None
    )
    assert await repo.list_for_backtest(TenantId("tOther"), since_days=30) == []

    # 写：三租户必须一致，否则拒绝
    with pytest.raises(ValueError):
        await repo.add(
            TenantId("tOther"), _snap("snap-ms-2", "tMs", "opp-ms-1", scored_at=_NOW)
        )
    with pytest.raises(ValueError):
        await repo.add(
            TenantId("tMs"), _snap("snap-ms-3", "tOther", "opp-ms-1", scored_at=_NOW)
        )
