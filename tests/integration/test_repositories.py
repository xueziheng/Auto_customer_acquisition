"""S2-4/S2-5 仓储集成测试（Opportunity / ScoreSnapshot / Handoff / Loss / Provenance）。

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

强化验收（监督复核要求 + S2-5）：
- 同 (tenant_id, need_id) 真正并发：两个独立会话 gather，恰好一个 commit 成功。
- ORM metadata 与 head(0002+0003) 逐表一致：十表列集合、11 索引名+列序、关键 unique/check/FK 名。
- 方法 tenant_id 与绑定租户不一致：读/list/latest/backtest 返回空、条件更新 False；
  add/update 对象租户与绑定租户不一致 → ValueError（硬边界 8 写侧）。
- 快照 add 要求 bound == method tenant == snapshot.tenant，否则 ValueError。
- Handoff：CRUD / find_pending / list_pending 按 requested_at 升序 / count 按员工 /
  accept_if_requested 两个独立会话并发仅一个 True。
- LossRecord：add 只追加；count 二维 GROUP BY + since_days；DB 只增触发器拒
  UPDATE/DELETE；add 三方租户一致。
- FieldProvenance：shared.Provenance 全字段往返；同字段多版本保留且 extracted_at
  新到旧；handoff/customer_verbatim 实体类型；租户隔离。

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
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.opportunities.permissions import OpportunityScope, ScopeLevel
from shared.errors import InvalidStateTransition
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    LossRecordId,
    OpportunityId,
    ProspectAccountId,
    ScoreSnapshotId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_USD = CurrencyCode("USD")

_MODULE_BY_SYMBOL = {
    "Opportunity": "domains.opportunities.models",
    "OpportunityState": "domains.opportunities.models",
    "LossReason": "domains.opportunities.models",
    "ScoreSnapshot": "domains.opportunities.models",
    "SortKey": "domains.opportunities.models",
    "HandoffPacket": "domains.opportunities.models",
    "HandoffState": "domains.opportunities.models",
    "HandoffTrigger": "domains.opportunities.models",
    "LossRecord": "domains.opportunities.models",
    "OpportunityRepositoryImpl": "infra.db.repositories.opportunities",
    "ScoreSnapshotRepositoryImpl": "infra.db.repositories.opportunities",
    "HandoffRepositoryImpl": "infra.db.repositories.opportunities",
    "LossRecordRepositoryImpl": "infra.db.repositories.opportunities",
    "FieldProvenanceRepositoryImpl": "infra.db.repositories.opportunities",
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
HandoffPacket = _load("HandoffPacket")
HandoffState = _load("HandoffState")
HandoffTrigger = _load("HandoffTrigger")
LossRecord = _load("LossRecord")


def _opp(opportunity_id: str, tenant_id: str, need_id: str, **overrides):
    """构造最小合法机会（可覆盖 state/owner 等字段）。"""
    product_category = overrides.pop("product_category", "hinges")
    created_at = overrides.pop("created_at", _NOW)
    account_name = overrides.pop("account_name", "Acme")
    country = overrides.pop("country", "US")
    return Opportunity(
        opportunity_id=OpportunityId(opportunity_id),
        tenant_id=TenantId(tenant_id),
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId(need_id),
        product_category=product_category,
        created_at=created_at,
        account_name=account_name,
        country=country,
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


async def test_list_scoped_applies_all_dimensions_states_and_limit_in_sql(
    repo_session: AsyncSession,
) -> None:
    """owner/country/category/states 均先 WHERE 再 LIMIT，顺序稳定且租户隔离。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    tenant = TenantId("tScopeOpp")
    repo = OpportunityRepositoryImpl(repo_session, tenant)
    rows = [
        _opp(
            "opp-sc-owner",
            "tScopeOpp",
            "need-sc-owner",
            owner=EmployeeId("sales-2"),
            country="US",
            product_category="hinges",
            state=OpportunityState.ASSIGNED,
            created_at=_NOW + timedelta(hours=5),
        ),
        _opp(
            "opp-sc-country",
            "tScopeOpp",
            "need-sc-country",
            owner=EmployeeId("sales-1"),
            country="DE",
            product_category="hinges",
            state=OpportunityState.ASSIGNED,
            created_at=_NOW + timedelta(hours=4),
        ),
        _opp(
            "opp-sc-category",
            "tScopeOpp",
            "need-sc-category",
            owner=EmployeeId("sales-1"),
            country="US",
            product_category="bolts",
            state=OpportunityState.ASSIGNED,
            created_at=_NOW + timedelta(hours=3),
        ),
        _opp(
            "opp-sc-a",
            "tScopeOpp",
            "need-sc-a",
            owner=EmployeeId("sales-1"),
            country="US",
            product_category="hinges",
            state=OpportunityState.ASSIGNED,
            created_at=_NOW,
        ),
        _opp(
            "opp-sc-b",
            "tScopeOpp",
            "need-sc-b",
            owner=EmployeeId("sales-1"),
            country="US",
            product_category="hinges",
            state=OpportunityState.CONTACTED,
            created_at=_NOW,
        ),
    ]
    for row in rows:
        await repo.add(row)
    other_repo = OpportunityRepositoryImpl(repo_session, TenantId("tScopeOppOther"))
    await other_repo.add(
        _opp(
            "opp-sc-cross",
            "tScopeOppOther",
            "need-sc-cross",
            owner=EmployeeId("sales-1"),
            country="US",
            product_category="hinges",
            created_at=_NOW + timedelta(hours=8),
        )
    )
    await repo_session.commit()

    owner_scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_owners=frozenset({EmployeeId("sales-1")}),
    )
    owner_rows = await repo.list_scoped(tenant, owner_scope, None, 20)
    assert {row.opportunity_id for row in owner_rows} == {
        OpportunityId("opp-sc-country"),
        OpportunityId("opp-sc-category"),
        OpportunityId("opp-sc-a"),
        OpportunityId("opp-sc-b"),
    }

    country_scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_countries=frozenset({"US"}),
    )
    country_rows = await repo.list_scoped(tenant, country_scope, None, 20)
    assert {row.opportunity_id for row in country_rows} == {
        OpportunityId("opp-sc-owner"),
        OpportunityId("opp-sc-category"),
        OpportunityId("opp-sc-a"),
        OpportunityId("opp-sc-b"),
    }

    category_scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_categories=frozenset({"hinges"}),
    )
    category_rows = await repo.list_scoped(tenant, category_scope, None, 20)
    assert {row.opportunity_id for row in category_rows} == {
        OpportunityId("opp-sc-owner"),
        OpportunityId("opp-sc-country"),
        OpportunityId("opp-sc-a"),
        OpportunityId("opp-sc-b"),
    }

    combined = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_owners=frozenset({EmployeeId("sales-1")}),
        allowed_countries=frozenset({"US"}),
        allowed_categories=frozenset({"hinges"}),
    )
    combined_rows = await repo.list_scoped(tenant, combined, None, 20)
    assert [row.opportunity_id for row in combined_rows] == [
        OpportunityId("opp-sc-a"),
        OpportunityId("opp-sc-b"),
    ]
    # 三条越界记录 created_at 更晚；limit=1 仍返回授权行，证明 WHERE 在 LIMIT 前。
    limited = await repo.list_scoped(tenant, combined, None, 1)
    assert [row.opportunity_id for row in limited] == [OpportunityId("opp-sc-a")]
    assigned_only = await repo.list_scoped(
        tenant, combined, [OpportunityState.ASSIGNED], 20
    )
    assert [row.opportunity_id for row in assigned_only] == [OpportunityId("opp-sc-a")]
    assert await repo.list_scoped(tenant, combined, [], 20) == []

    for empty_scope in (
        OpportunityScope(
            level=ScopeLevel.MANAGER,
            allowed_owners=frozenset(),
        ),
        OpportunityScope(
            level=ScopeLevel.MANAGER,
            allowed_countries=frozenset(),
        ),
        OpportunityScope(
            level=ScopeLevel.MANAGER,
            allowed_categories=frozenset(),
        ),
    ):
        assert await repo.list_scoped(tenant, empty_scope, None, 20) == []

    tenant_rows = await repo.list_scoped(
        tenant, OpportunityScope(level=ScopeLevel.TENANT), None, 20
    )
    assert len(tenant_rows) == 5
    assert OpportunityId("opp-sc-cross") not in {
        row.opportunity_id for row in tenant_rows
    }
    assert (
        await repo.list_scoped(
            TenantId("tScopeOppOther"),
            OpportunityScope(level=ScopeLevel.TENANT),
            None,
            20,
        )
        == []
    )
    for unsafe_scope in (
        OpportunityScope(),
        OpportunityScope(level=ScopeLevel.SYSTEM),
    ):
        assert await repo.list_scoped(tenant, unsafe_scope, None, 20) == []


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


# --- 强化验收：ORM metadata 与 head(0002+0003) 逐表一致（列 / 11 索引 / 关键约束名）--------


def test_orm_metadata_parity_with_head() -> None:
    """ORM metadata 与当前迁移 head 一致：列集合、索引名/列序、关键约束名。

    schema 仍由 Alembic 迁移管理（不用 create_all）；本断言防 ORM 与迁移漂移。
    """
    from infra.db.tables import Base

    metadata = Base.metadata

    expected_columns = {
        "approval_packages": {
            "tenant_id", "approval_id", "approval_type", "title",
            "proposed_change", "reason", "blast_radius", "created_at",
            "expires_at", "state", "proposed_by_run", "proposed_by_employee",
            "evidence_refs", "change_set_ref", "owner_employee", "decided_at",
            "decided_by", "decision_note", "applied_at", "apply_error",
        },
        "approval_applications": {
            "tenant_id", "approval_id", "idempotency_key", "created_at",
        },
        "directive_proposals": {
            "tenant_id", "proposal_id", "raw_text", "parsed_content",
            "interpretation_summary", "expected_behavior_changes", "parsed_by",
            "state", "created_at", "decided_at", "decided_by",
        },
        "directive_versions": {
            "tenant_id", "directive_id", "version", "content",
            "source_proposal_id", "activated_at", "activated_by",
            "superseded_at", "rollback_of",
        },
        "boss_directives": {
            "tenant_id", "directive_id", "version", "activated_at",
        },
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
        "handoff_escalations": {
            "escalation_id", "tenant_id", "handoff_id", "level",
            "escalated_at", "note",
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
            "delivered_at", "next_attempt_at", "last_error",
        },
        "outbox_deliveries": {
            "delivery_id", "tenant_id", "event_id", "handler_name", "status",
            "attempts", "next_attempt_at", "last_error", "delivered_at",
        },
        "employees": {
            "employee_id", "tenant_id", "name", "role", "created_at", "user_id",
            "team_id", "manager_id", "languages", "timezone", "is_active",
            "max_active_accounts",
        },
        "territory_assignments": {
            "assignment_id", "tenant_id", "employee_id", "priority", "effective_from",
            "countries", "product_categories", "need_categories", "buyer_types",
            "languages", "manager_id", "backup_employee_id", "effective_until",
        },
        "ownership_locks": {
            "lock_id", "tenant_id", "account_id", "owner", "locked_at", "locked_by_rule",
        },
        "ownership_transfer_history": {
            "transfer_id", "tenant_id", "account_id", "from_owner", "to_owner",
            "transferred_by", "transferred_at", "reason",
        },
        "workflow_runs": {
            "run_id", "tenant_id", "workflow_type", "workflow_version", "subject_ref",
            "current_step", "status", "created_at", "next_poll_at", "retry_count",
            "context", "last_error", "idempotency_key",
        },
        "workflow_steps": {
            "step_id", "run_id", "tenant_id", "step_name", "status", "data", "attempt",
            "error", "due_at", "idempotency_key", "created_at", "updated_at",
        },
        "outreach_campaigns": {
            "tenant_id", "campaign_id", "state", "current_version", "created_by",
            "created_at", "round_robin_cursor", "approval_id", "approved_by",
            "approved_at", "paused_reason",
        },
        "outreach_campaign_versions": {
            "tenant_id", "campaign_id", "version", "name", "markets",
            "target_entity_types", "allowed_categories", "sender_identity_ids",
            "daily_new_contact_limit", "daily_total_message_limit", "handoff_triggers",
            "stop_on_reply", "created_by", "created_at",
        },
        "outreach_sequence_steps": {
            "tenant_id", "campaign_id", "version", "step_number", "intent", "wait_days",
        },
        "outreach_enrollments": {
            "tenant_id", "enrollment_id", "campaign_id", "campaign_version", "account_id",
            "contact_point_id", "sending_identity_id", "state", "current_step",
            "next_send_at", "enrolled_at", "stopped_at", "stop_reason", "idempotency_key",
        },
        "outreach_suppressions": {
            "tenant_id", "suppression_id", "contact_point_id", "account_id", "reason",
            "occurred_at", "source_ref", "idempotency_key", "created_at",
        },
        "outreach_daily_quotas": {
            "tenant_id", "campaign_id", "on_day", "new_contacts_reserved",
            "messages_reserved",
        },
        "outreach_message_attempts": {
            "tenant_id", "attempt_id", "message_id", "campaign_id", "enrollment_id",
            "campaign_version", "step_number", "sending_identity_id", "idempotency_key",
            "state", "provider_ref", "failure_category", "created_at", "updated_at",
            "send_claimed_at", "deterministic_message_id", "idempotency_header",
        },
        "outreach_actions": {
            "tenant_id", "action_id", "action_key", "action", "entity_id", "actor_id",
            "occurred_at",
        },
        "tool_calls": {
            "tenant_id", "tool_call_id", "tool_id", "tool_version", "risk_level",
            "cost_class", "idempotency_key", "request_fingerprint",
            "fingerprint_version", "status", "duplicate_of", "lease_owner",
            "lease_expires_at", "attempt_count", "run_id", "user_id", "campaign_id",
            "message_attempt_id", "provider_ref", "error_category", "retry_after_at",
            "created_at", "updated_at", "completed_at",
        },
        "tool_call_events": {
            "tenant_id", "event_id", "tool_call_id", "stage", "outcome", "rule",
            "category", "actor_id", "run_id", "campaign_id", "message_attempt_id",
            "occurred_at", "duration_ms", "cost_note",
        },
        "email_feedback_cursors": {
            "tenant_id", "mailbox_alias", "provider_cursor", "version",
            "bootstrap_started_at", "last_succeeded_at",
        },
        "email_feedback_receipts": {
            "tenant_id", "mailbox_alias", "provider_event_id", "ordinal", "kind",
            "item_fingerprint", "occurred_at", "result", "attempt_id", "enrollment_id", "account_id",
            "contact_point_id", "sending_identity_id", "created_at",
        },
        "email_feedback_quarantines": {
            "tenant_id", "mailbox_alias", "provider_event_id", "reason",
            "provider_ref_digest", "created_at",
        },
        "unsubscribe_tokens": {
            "tenant_id", "nonce_sha256", "contact_point_id", "message_attempt_id",
            "key_id", "expires_at", "consumed_at", "created_at",
        },
        "raw_artifacts": {
            "tenant_id", "artifact_id", "kind", "content_hash", "size_bytes",
            "mime_type", "object_key", "uploaded_by", "uploaded_at",
        },
        "artifacts": {
            "tenant_id", "artifact_id", "kind", "content_hash", "size_bytes",
            "mime_type", "object_key", "workflow_run_id", "subject_ref",
            "sequence_number", "idempotency_key", "generated_by", "generated_at",
        },
        "notification_jobs": {"tenant_id", "notification_job_id", "source_event_fingerprint", "source_event", "recipient_employee_id", "priority", "context_kind", "primary_id", "secondary_id", "reason_code", "level", "dedup_key", "status", "available_at", "lease_owner", "lease_token", "lease_expires_at", "attempt_count", "last_error", "created_at", "completed_at"},
        "in_app_notifications": {"tenant_id", "notification_id", "recipient_employee_id", "priority", "title", "context_kind", "primary_id", "secondary_id", "reason_code", "level", "relative_link", "source_job_id", "created_at", "read_at"},
        "need_clusters": {
            "tenant_id", "cluster_id", "category", "keywords", "countries",
            "total_potential_quantity", "recurring_demand", "created_at",
            "updated_at",
        },
        "need_cluster_members": {
            "tenant_id", "cluster_id", "need_id", "assigned_at",
        },
        "validated_needs": {
            "tenant_id", "need_id", "account_id", "product_category",
            "source_message_id", "source_conversation_id", "status", "created_at",
            "application", "material", "size_spec", "quantity", "packaging",
            "destination", "required_by", "target_price", "current_supply_issue",
            "certification_required", "confirmed_by", "cluster_id",
        },
        "prospect_accounts": {
            "tenant_id", "account_id", "name", "country", "website_domain",
            "entity_type", "industry", "size_hint", "source_signal_refs", "created_at",
        },
        "prospect_contacts": {
            "tenant_id", "contact_id", "account_id", "full_name", "role_title",
            "language", "created_at",
        },
        "contact_points": {
            "tenant_id", "contact_point_id", "contact_id", "kind", "value",
            "value_hash", "verification_status", "verified_at",
            "verification_provider", "verification_checked_at",
            "verification_cost_note", "enrichment_cost_note", "created_at",
        },
        "contact_legal_basis": {
            "tenant_id", "contact_point_id", "basis", "subject_type", "contact_type",
            "source", "source_url", "collected_at", "assessment_ref",
        },
        "prospecting_erasure_suppressions": {
            "tenant_id", "value_hash", "erased_at",
        },
        "work_uploads": {
            "tenant_id", "upload_id", "artifact_id", "employee_id",
            "source_kind", "status", "occurred_at", "customer_timezone",
            "account_id", "opportunity_id", "need_id", "created_at",
        },
        "extracted_facts": {
            "tenant_id", "extraction_id", "upload_id", "payload",
            "extracted_by", "created_at",
        },
        "employee_confirmations": {
            "tenant_id", "confirmation_id", "extraction_id", "revision",
            "payload", "confirmed_by", "confirmed_at",
        },
    }
    for table, cols in expected_columns.items():
        assert table in metadata.tables, f"缺表 {table}"
        actual = {c.name for c in metadata.tables[table].columns}
        assert actual == cols, f"{table} 列集合不一致：{sorted(actual ^ cols)}"

    expected_indexes = {
        "ix_approval_packages_tenant_state_expiry": (
            "tenant_id", "state", "expires_at", "approval_id",
        ),
        "ix_approval_packages_tenant_change_set": (
            "tenant_id", "change_set_ref", "created_at",
        ),
        "uq_approval_packages_pending_change_set": (
            "tenant_id", "change_set_ref",
        ),
        "ix_directive_proposals_tenant_state_created": (
            "tenant_id", "state", "created_at", "proposal_id",
        ),
        "ix_directive_versions_tenant_version": ("tenant_id", "version"),
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
        "ix_employees_tenant_role": ("tenant_id", "role"),
        "ix_employees_tenant_active": ("tenant_id", "is_active"),
        "ix_territory_tenant_priority": ("tenant_id", "priority"),
        "ix_transfer_tenant_account": ("tenant_id", "account_id"),
        "ix_workflow_runs_tenant_status_poll": ("tenant_id", "status", "next_poll_at"),
        "ix_workflow_steps_tenant_status_due": ("tenant_id", "status", "due_at"),
        "ix_sending_reputation_tenant_identity_occurred": (
            "tenant_id", "identity_id", "occurred_at",
        ),
        "ix_outreach_campaigns_tenant_state_created": (
            "tenant_id", "state", "created_at", "campaign_id",
        ),
        "uq_outreach_enrollments_active_account": ("tenant_id", "account_id"),
        "ix_outreach_enrollments_tenant_campaign_state": (
            "tenant_id", "campaign_id", "state", "enrolled_at", "enrollment_id",
        ),
        "ix_outreach_enrollments_tenant_contact_state": (
            "tenant_id", "contact_point_id", "state",
        ),
        "ix_outreach_suppressions_tenant_contact": (
            "tenant_id", "contact_point_id", "occurred_at",
        ),
        "ix_outreach_suppressions_tenant_account": (
            "tenant_id", "account_id", "occurred_at",
        ),
        "ix_outreach_attempts_tenant_enrollment_created": (
            "tenant_id", "enrollment_id", "created_at", "attempt_id",
        ),
        "uq_outreach_attempts_tenant_message_id": (
            "tenant_id", "deterministic_message_id",
        ),
        "uq_outreach_attempts_tenant_idempotency_header": (
            "tenant_id", "idempotency_header",
        ),
        "ix_email_feedback_receipts_tenant_mailbox_created": (
            "tenant_id", "mailbox_alias", "created_at", "provider_event_id",
        ),
        "ix_email_feedback_quarantines_tenant_created": (
            "tenant_id", "created_at", "provider_event_id",
        ),
        "ix_unsubscribe_tokens_tenant_attempt": (
            "tenant_id", "message_attempt_id", "created_at",
        ),
        "uq_tool_calls_tenant_tool_key": (
            "tenant_id", "tool_id", "idempotency_key",
        ),
        "ix_tool_calls_tenant_status_retry": (
            "tenant_id", "status", "retry_after_at", "updated_at",
        ),
        "ix_tool_call_events_tenant_call_occurred": (
            "tenant_id", "tool_call_id", "occurred_at", "event_id",
        ),
        "ix_notification_jobs_tenant_due": ("tenant_id", "status", "available_at"),
        "ix_in_app_notifications_recipient_created": ("tenant_id", "recipient_employee_id", "created_at", "notification_id"),
        "uq_need_hypotheses_active_account_category": (
            "tenant_id", "account_id", "category",
        ),
        "ix_need_clusters_tenant_category": (
            "tenant_id", "category", "created_at", "cluster_id",
        ),
        "uq_need_cluster_members_need": ("tenant_id", "need_id"),
        "ix_validated_need_field_history_need": (
            "tenant_id", "need_id", "changed_at",
        ),
        "uq_prospect_accounts_domain": ("tenant_id", "website_domain"),
        "ix_prospect_accounts_name": ("tenant_id", "country", "name"),
        "ix_prospect_contacts_account": (
            "tenant_id", "account_id", "created_at", "contact_id",
        ),
        "ix_commitments_tenant_owner_status_due": (
            "tenant_id", "owner", "status", "due_at", "commitment_id",
        ),
        "ix_work_uploads_tenant_employee_created": (
            "tenant_id", "employee_id", "created_at", "upload_id",
        ),
    }
    actual_indexes: dict[str, tuple[str, ...]] = {}
    for tbl in metadata.tables.values():
        for idx in tbl.indexes:
            if idx.name is not None:
                actual_indexes[str(idx.name)] = tuple(idx.columns.keys())
    assert actual_indexes == expected_indexes, f"索引不一致：{actual_indexes}"

    expected_constraints = {
        "approval_packages": {
            "pk_approval_packages", "ck_approval_packages_state",
            "ck_approval_packages_jsonb", "ck_approval_packages_core_nonblank",
            "ck_approval_packages_expiry", "ck_approval_packages_decision",
            "ck_approval_packages_application",
        },
        "approval_applications": {
            "pk_approval_applications", "uq_approval_applications_key",
            "fk_approval_applications_package",
            "ck_approval_applications_nonblank",
        },
        "directive_proposals": {
            "pk_directive_proposals", "ck_directive_proposals_state",
            "ck_directive_proposals_jsonb", "ck_directive_proposals_core_nonblank",
            "ck_directive_proposals_decision",
        },
        "directive_versions": {
            "pk_directive_versions", "uq_directive_versions_tenant_version",
            "uq_directive_versions_pointer", "fk_directive_versions_proposal",
            "ck_directive_versions_version", "ck_directive_versions_content_jsonb",
            "ck_directive_versions_core_nonblank",
            "ck_directive_versions_superseded_at", "ck_directive_versions_rollback",
        },
        "boss_directives": {
            "pk_boss_directives", "fk_boss_directives_version",
            "ck_boss_directives_core",
        },
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
        "handoffs": {"fk_handoffs_opportunity", "uq_handoffs_tenant_handoff"},
        "handoff_escalations": {
            "fk_handoff_escalations_handoff",
            "uq_handoff_escalations_tenant_handoff_level",
        },
        "loss_records": {"fk_loss_records_opportunity"},
        "outbox_events": {
            "ck_outbox_attempt_min", "ck_outbox_status", "uq_outbox_events_tenant_event",
        },
        "outbox_deliveries": {
            "ck_outbox_deliveries_status",
            "uq_outbox_deliveries_tenant_event_handler",
            "fk_outbox_deliveries_event",
        },
        "provenance_records": set(),
        "employees": {"uq_employees_tenant_user", "uq_employees_tenant_employee"},
        "territory_assignments": {
            "fk_territory_employee", "fk_territory_manager", "fk_territory_backup",
        },
        "ownership_locks": {"uq_ownership_locks_tenant_account", "fk_ownership_locks_owner"},
        "ownership_transfer_history": {
            "ck_transfer_reason_nonblank",
            "fk_transfer_from_owner", "fk_transfer_to_owner", "fk_transfer_transferred_by",
        },
        "workflow_runs": {"uq_workflow_runs_tenant_key", "uq_workflow_runs_tenant_run"},
        "workflow_steps": {"uq_workflow_steps_tenant_key", "fk_workflow_steps_run"},
        "outreach_campaigns": {
            "pk_outreach_campaigns", "ck_outreach_campaign_state",
            "ck_outreach_campaign_version", "ck_outreach_campaign_cursor",
            "ck_outreach_campaign_approval_tuple",
        },
        "outreach_campaign_versions": {
            "pk_outreach_campaign_versions", "fk_outreach_campaign_versions_campaign",
            "ck_outreach_version_number", "ck_outreach_version_quotas",
            "ck_outreach_version_stop_on_reply",
        },
        "outreach_sequence_steps": {
            "pk_outreach_sequence_steps", "fk_outreach_steps_version",
            "ck_outreach_step_number", "ck_outreach_step_intent", "ck_outreach_step_wait",
        },
        "outreach_enrollments": {
            "pk_outreach_enrollments", "uq_outreach_enrollments_tenant_key",
            "fk_outreach_enrollments_version", "ck_outreach_enrollment_state",
            "ck_outreach_enrollment_step", "ck_outreach_enrollment_stop_fields",
        },
        "outreach_suppressions": {
            "pk_outreach_suppressions", "uq_outreach_suppressions_tenant_key",
            "ck_outreach_suppression_exact_target", "ck_outreach_suppression_reason",
        },
        "outreach_daily_quotas": {
            "pk_outreach_daily_quotas", "fk_outreach_quotas_campaign",
            "ck_outreach_quota_nonnegative",
        },
        "outreach_message_attempts": {
            "pk_outreach_message_attempts", "uq_outreach_attempts_tenant_key",
            "fk_outreach_attempts_enrollment", "fk_outreach_attempts_version",
            "ck_outreach_attempt_step", "ck_outreach_attempt_state_fields",
            "ck_outreach_attempt_correlation_pair",
            "ck_outreach_attempt_correlation_grammar",
        },
        "outreach_actions": {
            "pk_outreach_actions", "uq_outreach_actions_tenant_key",
        },
        "tool_calls": {
            "pk_tool_calls", "uq_tool_calls_tenant_call", "fk_tool_calls_duplicate",
            "ck_tool_calls_status", "ck_tool_calls_risk", "ck_tool_calls_cost",
            "ck_tool_calls_safe_labels",
            "ck_tool_calls_fingerprint_pair", "ck_tool_calls_canonical_fields",
            "ck_tool_calls_duplicate_fields", "ck_tool_calls_lease_fields",
            "ck_tool_calls_attempt_count", "ck_tool_calls_result_fields",
            "ck_tool_calls_provider_ref",
        },
        "tool_call_events": {
            "pk_tool_call_events", "fk_tool_call_events_call",
            "ck_tool_call_events_duration", "ck_tool_call_events_category",
            "ck_tool_call_events_safe_labels",
        },
        "email_feedback_cursors": {
            "pk_email_feedback_cursors", "ck_email_feedback_cursor_tenant",
            "ck_email_feedback_cursor_mailbox", "ck_email_feedback_cursor_version",
            "ck_email_feedback_cursor_value",
        },
        "email_feedback_receipts": {
            "pk_email_feedback_receipts", "fk_email_feedback_receipts_cursor",
            "fk_email_feedback_receipts_attempt", "ck_email_feedback_receipt_tenant",
            "ck_email_feedback_receipt_mailbox", "ck_email_feedback_receipt_event",
            "ck_email_feedback_receipt_ordinal", "ck_email_feedback_receipt_kind",
            "ck_email_feedback_receipt_result", "ck_email_feedback_receipt_target",
        },
        "email_feedback_quarantines": {
            "pk_email_feedback_quarantines", "fk_email_feedback_quarantine_receipt",
            "ck_email_feedback_quarantine_reason",
            "ck_email_feedback_quarantine_digest",
        },
        "unsubscribe_tokens": {
            "pk_unsubscribe_tokens", "fk_unsubscribe_token_attempt",
            "ck_unsubscribe_token_tenant", "ck_unsubscribe_token_nonce",
            "ck_unsubscribe_token_contact", "ck_unsubscribe_token_attempt",
            "ck_unsubscribe_token_key", "ck_unsubscribe_token_expiry",
            "ck_unsubscribe_token_consumed",
        },
        "raw_artifacts": {
            "pk_raw_artifacts", "uq_raw_artifacts_tenant_kind_hash",
            "ck_raw_artifacts_tenant", "ck_raw_artifacts_id",
            "ck_raw_artifacts_hash", "ck_raw_artifacts_size",
            "ck_raw_artifacts_kind_mime", "ck_raw_artifacts_object_key",
            "ck_raw_artifacts_uploader",
        },
        "artifacts": {
            "pk_artifacts", "uq_artifacts_tenant_key", "ck_artifacts_tenant",
            "ck_artifacts_id", "ck_artifacts_hash", "ck_artifacts_size",
            "ck_artifacts_kind_mime", "ck_artifacts_object_key",
            "ck_artifacts_run", "ck_artifacts_subject", "ck_artifacts_sequence",
            "ck_artifacts_idempotency", "ck_artifacts_generated_by",
        },
        "notification_jobs": {
            "pk_notification_jobs", "uq_notification_jobs_source_recipient_kind",
            "ck_notification_jobs_status", "ck_notification_jobs_priority",
            "ck_notification_jobs_attempt_count",
        },
        "in_app_notifications": {
            "pk_in_app_notifications", "uq_in_app_notifications_source_job",
            "fk_in_app_notifications_job", "ck_in_app_notifications_priority",
        },
        "need_clusters": {
            "pk_need_clusters", "ck_need_clusters_core_nonblank",
            "ck_need_clusters_arrays_jsonb", "ck_need_clusters_quantity_nonnegative",
        },
        "need_cluster_members": {
            "pk_need_cluster_members", "fk_need_cluster_members_cluster",
            "fk_need_cluster_members_need",
        },
        "validated_needs": {"fk_validated_needs_cluster"},
        "prospect_accounts": {
            "pk_prospect_accounts", "ck_prospect_accounts_core_nonblank",
            "ck_prospect_accounts_optional_nonblank",
            "ck_prospect_accounts_source_refs_jsonb",
        },
        "prospect_contacts": {
            "pk_prospect_contacts", "fk_prospect_contacts_account",
            "ck_prospect_contacts_core_nonblank",
            "ck_prospect_contacts_optional_nonblank",
        },
        "contact_points": {
            "pk_contact_points", "fk_contact_points_contact",
            "uq_contact_points_value_hash", "ck_contact_points_kind",
            "ck_contact_points_verification_status", "ck_contact_points_value_hash",
            "ck_contact_points_verified_pair",
            "ck_contact_points_verification_observation",
            "ck_contact_points_core_nonblank", "ck_contact_points_optional_nonblank",
        },
        "contact_legal_basis": {
            "pk_contact_legal_basis", "fk_contact_legal_basis_contact_point",
            "ck_contact_legal_basis_basis", "ck_contact_legal_basis_subject_type",
            "ck_contact_legal_basis_contact_type",
            "ck_contact_legal_basis_li_assessment",
            "ck_contact_legal_basis_core_nonblank",
            "ck_contact_legal_basis_optional_nonblank",
        },
        "prospecting_erasure_suppressions": {
            "pk_prospecting_erasure_suppressions",
            "ck_prospecting_erasure_tenant_nonblank",
            "ck_prospecting_erasure_value_hash",
        },
        "work_uploads": {
            "pk_work_uploads", "uq_work_uploads_artifact",
            "fk_work_uploads_artifact", "ck_work_uploads_source_kind",
            "ck_work_uploads_status", "ck_work_uploads_core_nonblank",
        },
        "extracted_facts": {
            "pk_extracted_facts", "uq_extracted_facts_upload",
            "fk_extracted_facts_upload", "ck_extracted_facts_payload_jsonb",
            "ck_extracted_facts_core_nonblank",
        },
        "employee_confirmations": {
            "pk_employee_confirmations", "uq_employee_confirmations_extraction",
            "fk_employee_confirmations_extraction",
            "ck_employee_confirmations_revision",
            "ck_employee_confirmations_payload_jsonb",
            "ck_employee_confirmations_core_nonblank",
        },
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


# --- Batch 1：Handoff 仓储 ------------------------------------------------------


def _handoff(handoff_id: str, tenant_id: str, opportunity_id: str, *, requested_at=_NOW, **overrides):
    """构造最小合法接管包（可覆盖 assigned_to/state 等字段）。"""
    return HandoffPacket(
        handoff_id=HandoffId(handoff_id),
        tenant_id=TenantId(tenant_id),
        opportunity_id=OpportunityId(opportunity_id),
        trigger=HandoffTrigger.QUOTE_REQUESTED,
        requested_at=requested_at,
        account_name="Acme",
        country="US",
        why_valuable="正在扩建第二座工厂",
        customer_verbatim="we need hinges",
        **overrides,
    )


async def _seed_opp(session: AsyncSession, opp_id: str, tenant_id: str, need_id: str) -> None:
    """seed 一个机会（handoffs/loss_records 复合 FK 引用必需）。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    await OpportunityRepositoryImpl(session, TenantId(tenant_id)).add(
        _opp(opp_id, tenant_id, need_id)
    )
    await session.commit()


async def test_handoff_crud_roundtrip(repo_session: AsyncSession) -> None:
    """add → get → update（assigned_to 落库）→ get。"""
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    await _seed_opp(repo_session, "opp-ho-1", "tHo", "need-ho-1")
    repo = HandoffRepositoryImpl(repo_session, TenantId("tHo"))
    packet = _handoff("ho-crud-1", "tHo", "opp-ho-1")
    await repo.add(packet)
    await repo_session.commit()

    found = await repo.get(TenantId("tHo"), HandoffId("ho-crud-1"))
    assert found is not None
    assert found.account_name == "Acme"
    assert found.state == HandoffState.REQUESTED

    packet.assigned_to = EmployeeId("emp-1")
    await repo.update(packet)
    await repo_session.commit()

    found = await repo.get(TenantId("tHo"), HandoffId("ho-crud-1"))
    assert found is not None
    assert found.assigned_to == EmployeeId("emp-1")


async def test_handoff_find_pending_for_opportunity(repo_session: AsyncSession) -> None:
    """未完成接管可查（幂等）；accept 后不再 pending。"""
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    await _seed_opp(repo_session, "opp-ho2-1", "tHo2", "need-ho2-1")
    repo = HandoffRepositoryImpl(repo_session, TenantId("tHo2"))
    await repo.add(_handoff("ho-pend-1", "tHo2", "opp-ho2-1"))
    await repo_session.commit()

    pending = await repo.find_pending_for_opportunity(
        TenantId("tHo2"), OpportunityId("opp-ho2-1")
    )
    assert pending is not None
    assert pending.handoff_id == HandoffId("ho-pend-1")

    ok = await repo.accept_if_requested(
        TenantId("tHo2"), HandoffId("ho-pend-1"), EmployeeId("emp-1"), _NOW
    )
    assert ok is True
    await repo_session.commit()
    assert (
        await repo.find_pending_for_opportunity(TenantId("tHo2"), OpportunityId("opp-ho2-1"))
        is None
    )


async def test_handoff_list_pending_ordered_by_requested_at(repo_session: AsyncSession) -> None:
    """待接管队列按 requested_at 升序（最久等待最前），不按分数。"""
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    for opp, need in (("opp-ho3-1", "need-ho3-1"), ("opp-ho3-2", "need-ho3-2"), ("opp-ho3-3", "need-ho3-3")):
        await _seed_opp(repo_session, opp, "tHo3", need)
    repo = HandoffRepositoryImpl(repo_session, TenantId("tHo3"))
    old = _NOW - timedelta(hours=2)
    mid = _NOW - timedelta(hours=1)
    await repo.add(_handoff("ho-p1", "tHo3", "opp-ho3-1", requested_at=old))
    await repo.add(_handoff("ho-p2", "tHo3", "opp-ho3-2", requested_at=_NOW))
    await repo.add(_handoff("ho-p3", "tHo3", "opp-ho3-3", requested_at=mid))
    await repo_session.commit()

    pending = await repo.list_pending(TenantId("tHo3"), limit=10)
    assert [h.handoff_id for h in pending] == [
        HandoffId("ho-p1"),
        HandoffId("ho-p3"),
        HandoffId("ho-p2"),
    ]


async def test_list_pending_scoped_uses_assignment_snapshot_and_authoritative_join(
    repo_session: AsyncSession,
) -> None:
    """pending scope 先 WHERE 再 LIMIT；owner 看 snapshot，国家/品类看关联机会。"""
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    tenant = TenantId("tScopeHo")
    opportunity_repo = OpportunityRepositoryImpl(repo_session, tenant)
    opportunity_rows = [
        _opp(
            "opp-hs-owner",
            "tScopeHo",
            "need-hs-owner",
            owner=EmployeeId("current-owner-x"),
            country="US",
            product_category="hinges",
        ),
        _opp(
            "opp-hs-country",
            "tScopeHo",
            "need-hs-country",
            country="DE",
            product_category="hinges",
        ),
        _opp(
            "opp-hs-category",
            "tScopeHo",
            "need-hs-category",
            country="US",
            product_category="bolts",
        ),
        _opp(
            "opp-hs-a",
            "tScopeHo",
            "need-hs-a",
            owner=EmployeeId("different-current-owner"),
            country="US",
            product_category="hinges",
        ),
        _opp(
            "opp-hs-b",
            "tScopeHo",
            "need-hs-b",
            owner=EmployeeId("different-current-owner"),
            country="US",
            product_category="hinges",
        ),
        _opp(
            "opp-hs-accepted",
            "tScopeHo",
            "need-hs-accepted",
            country="US",
            product_category="hinges",
        ),
    ]
    for row in opportunity_rows:
        await opportunity_repo.add(row)

    other_tenant = TenantId("tScopeHoOther")
    other_opportunity_repo = OpportunityRepositoryImpl(repo_session, other_tenant)
    await other_opportunity_repo.add(
        _opp(
            "opp-hs-cross",
            "tScopeHoOther",
            "need-hs-cross",
            country="US",
            product_category="hinges",
        )
    )
    await repo_session.commit()

    repo = HandoffRepositoryImpl(repo_session, tenant)
    packets = [
        _handoff(
            "hand-hs-owner",
            "tScopeHo",
            "opp-hs-owner",
            requested_at=_NOW - timedelta(hours=5),
            assigned_to=EmployeeId("sales-2"),
        ),
        # packet.country 固定为 US，但关联机会是 DE；国家 ABAC 必须排除它。
        _handoff(
            "hand-hs-country",
            "tScopeHo",
            "opp-hs-country",
            requested_at=_NOW - timedelta(hours=4),
            assigned_to=EmployeeId("sales-1"),
        ),
        _handoff(
            "hand-hs-category",
            "tScopeHo",
            "opp-hs-category",
            requested_at=_NOW - timedelta(hours=4),
            assigned_to=EmployeeId("sales-1"),
        ),
        _handoff(
            "hand-hs-a",
            "tScopeHo",
            "opp-hs-a",
            requested_at=_NOW - timedelta(hours=3),
            assigned_to=EmployeeId("sales-1"),
        ),
        _handoff(
            "hand-hs-b",
            "tScopeHo",
            "opp-hs-b",
            requested_at=_NOW - timedelta(hours=3),
            assigned_to=EmployeeId("sales-1"),
        ),
        _handoff(
            "hand-hs-accepted",
            "tScopeHo",
            "opp-hs-accepted",
            requested_at=_NOW - timedelta(hours=6),
            assigned_to=EmployeeId("sales-1"),
        ),
    ]
    for packet in packets:
        await repo.add(packet)
    other_handoff_repo = HandoffRepositoryImpl(repo_session, other_tenant)
    await other_handoff_repo.add(
        _handoff(
            "hand-hs-cross",
            "tScopeHoOther",
            "opp-hs-cross",
            requested_at=_NOW - timedelta(hours=8),
            assigned_to=EmployeeId("sales-1"),
        )
    )
    await repo_session.commit()
    assert await repo.accept_if_requested(
        tenant,
        HandoffId("hand-hs-accepted"),
        EmployeeId("sales-1"),
        _NOW,
    )
    await repo_session.commit()

    owner_scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_owners=frozenset({EmployeeId("sales-1")}),
    )
    owner_rows = await repo.list_pending_scoped(tenant, owner_scope, 20)
    assert {row.handoff_id for row in owner_rows} == {
        HandoffId("hand-hs-country"),
        HandoffId("hand-hs-category"),
        HandoffId("hand-hs-a"),
        HandoffId("hand-hs-b"),
    }

    country_scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_countries=frozenset({"US"}),
    )
    country_rows = await repo.list_pending_scoped(tenant, country_scope, 20)
    assert {row.handoff_id for row in country_rows} == {
        HandoffId("hand-hs-owner"),
        HandoffId("hand-hs-category"),
        HandoffId("hand-hs-a"),
        HandoffId("hand-hs-b"),
    }

    category_scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_categories=frozenset({"hinges"}),
    )
    category_rows = await repo.list_pending_scoped(tenant, category_scope, 20)
    assert {row.handoff_id for row in category_rows} == {
        HandoffId("hand-hs-owner"),
        HandoffId("hand-hs-country"),
        HandoffId("hand-hs-a"),
        HandoffId("hand-hs-b"),
    }

    combined = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_owners=frozenset({EmployeeId("sales-1")}),
        allowed_countries=frozenset({"US"}),
        allowed_categories=frozenset({"hinges"}),
    )
    combined_rows = await repo.list_pending_scoped(tenant, combined, 20)
    assert [row.handoff_id for row in combined_rows] == [
        HandoffId("hand-hs-a"),
        HandoffId("hand-hs-b"),
    ]
    # 更老的越界/accepted 行都排在前面；limit=1 仍返回授权 pending 行。
    limited = await repo.list_pending_scoped(tenant, combined, 1)
    assert [row.handoff_id for row in limited] == [HandoffId("hand-hs-a")]

    for empty_scope in (
        OpportunityScope(
            level=ScopeLevel.MANAGER,
            allowed_owners=frozenset(),
        ),
        OpportunityScope(
            level=ScopeLevel.MANAGER,
            allowed_countries=frozenset(),
        ),
        OpportunityScope(
            level=ScopeLevel.MANAGER,
            allowed_categories=frozenset(),
        ),
    ):
        assert await repo.list_pending_scoped(tenant, empty_scope, 20) == []

    tenant_rows = await repo.list_pending_scoped(
        tenant, OpportunityScope(level=ScopeLevel.TENANT), 20
    )
    assert len(tenant_rows) == 5
    assert HandoffId("hand-hs-accepted") not in {
        row.handoff_id for row in tenant_rows
    }
    assert HandoffId("hand-hs-cross") not in {
        row.handoff_id for row in tenant_rows
    }
    assert (
        await repo.list_pending_scoped(
            other_tenant,
            OpportunityScope(level=ScopeLevel.TENANT),
            20,
        )
        == []
    )
    for unsafe_scope in (
        OpportunityScope(),
        OpportunityScope(level=ScopeLevel.SYSTEM),
    ):
        assert await repo.list_pending_scoped(tenant, unsafe_scope, 20) == []


async def test_handoff_count_pending_by_employee(repo_session: AsyncSession) -> None:
    """待接管按员工计数。"""
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    for opp, need in (("opp-ho4-1", "need-ho4-1"), ("opp-ho4-2", "need-ho4-2"), ("opp-ho4-3", "need-ho4-3")):
        await _seed_opp(repo_session, opp, "tHo4", need)
    repo = HandoffRepositoryImpl(repo_session, TenantId("tHo4"))
    await repo.add(_handoff("ho-c1", "tHo4", "opp-ho4-1", assigned_to=EmployeeId("emp-1")))
    await repo.add(_handoff("ho-c2", "tHo4", "opp-ho4-2", assigned_to=EmployeeId("emp-1")))
    await repo.add(_handoff("ho-c3", "tHo4", "opp-ho4-3", assigned_to=EmployeeId("emp-2")))
    await repo_session.commit()

    counts = await repo.count_pending_by_employee(TenantId("tHo4"))
    assert counts == {"emp-1": 2, "emp-2": 1}


async def test_handoff_accept_if_requested_atomic(db_url: str) -> None:
    """两个独立会话并发 accept：恰好一个 True、一个 False（WHERE state='requested' 原子）。"""
    from infra.db.session import create_engine_from

    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    engine = create_engine_from(db_url)
    session_a = AsyncSession(bind=engine, expire_on_commit=False)
    session_b = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        await _seed_opp(session_a, "opp-acc-1", "tAcc", "need-acc-1")
        await HandoffRepositoryImpl(session_a, TenantId("tAcc")).add(
            _handoff("ho-acc-1", "tAcc", "opp-acc-1")
        )
        await session_a.commit()

        repo_a = HandoffRepositoryImpl(session_a, TenantId("tAcc"))
        repo_b = HandoffRepositoryImpl(session_b, TenantId("tAcc"))

        async def _try_accept(session: AsyncSession, repo) -> bool:
            ok = await repo.accept_if_requested(
                TenantId("tAcc"), HandoffId("ho-acc-1"), EmployeeId("emp-1"), _NOW
            )
            await session.commit()
            return ok

        results = await asyncio.gather(
            _try_accept(session_a, repo_a), _try_accept(session_b, repo_b)
        )
        assert sorted(results) == [False, True], f"应恰好一个 accept 成功：{results}"

        found = await repo_a.get(TenantId("tAcc"), HandoffId("ho-acc-1"))
        assert found is not None
        assert found.state == HandoffState.ACCEPTED
    finally:
        await session_a.close()
        await session_b.close()
        await engine.dispose()


async def test_handoff_tenant_binding_mismatch(repo_session: AsyncSession) -> None:
    """方法租户与绑定不一致：读空、条件更新 False、写拒绝。"""
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    await _seed_opp(repo_session, "opp-hmb-1", "tHmbA", "need-hmb-1")
    repo_a = HandoffRepositoryImpl(repo_session, TenantId("tHmbA"))
    await repo_a.add(_handoff("ho-hmb-1", "tHmbA", "opp-hmb-1"))
    await repo_session.commit()

    assert await repo_a.get(TenantId("tHmbB"), HandoffId("ho-hmb-1")) is None
    assert (
        await repo_a.find_pending_for_opportunity(TenantId("tHmbB"), OpportunityId("opp-hmb-1"))
        is None
    )
    assert await repo_a.list_pending(TenantId("tHmbB"), limit=10) == []
    assert await repo_a.count_pending_by_employee(TenantId("tHmbB")) == {}
    assert (
        await repo_a.accept_if_requested(
            TenantId("tHmbB"), HandoffId("ho-hmb-1"), EmployeeId("emp-1"), _NOW
        )
        is False
    )

    with pytest.raises(ValueError):
        await repo_a.add(_handoff("ho-hmb-2", "tHmbB", "opp-hmb-2"))
    with pytest.raises(ValueError):
        await repo_a.update(_handoff("ho-hmb-1", "tHmbB", "opp-hmb-1"))


async def test_handoff_escalation_append_and_tenant_isolation(
    repo_session: AsyncSession,
) -> None:
    """不同 level 均追加；方法租户错配拒绝，跨租户 handoff 由复合 FK 拒绝。"""
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    await _seed_opp(repo_session, "opp-esc-a", "tEscA", "need-esc-a")
    await _seed_opp(repo_session, "opp-esc-b", "tEscB", "need-esc-b")
    repo_a = HandoffRepositoryImpl(repo_session, TenantId("tEscA"))
    repo_b = HandoffRepositoryImpl(repo_session, TenantId("tEscB"))
    await repo_a.add(_handoff("hand-esc-a", "tEscA", "opp-esc-a"))
    await repo_b.add(_handoff("hand-esc-b", "tEscB", "opp-esc-b"))
    await repo_session.commit()

    await repo_a.record_escalation(
        TenantId("tEscA"), HandoffId("hand-esc-a"), 1, _NOW
    )
    await repo_a.record_escalation(
        TenantId("tEscA"), HandoffId("hand-esc-a"), 2, _NOW + timedelta(minutes=5)
    )
    await repo_session.commit()
    rows = (
        await repo_session.execute(
            text(
                "SELECT level FROM handoff_escalations "
                "WHERE tenant_id = :tenant AND handoff_id = :handoff ORDER BY level"
            ),
            {"tenant": "tEscA", "handoff": "hand-esc-a"},
        )
    ).scalars().all()
    assert rows == [1, 2]

    with pytest.raises(ValueError):
        await repo_a.record_escalation(
            TenantId("tEscB"), HandoffId("hand-esc-a"), 3, _NOW
        )
    await repo_session.rollback()
    with pytest.raises(IntegrityError):
        await repo_a.record_escalation(
            TenantId("tEscA"), HandoffId("hand-esc-b"), 3, _NOW
        )
        await repo_session.commit()
    await repo_session.rollback()


async def test_handoff_escalation_service_duplicate_exact_constraint_noop(
    db_url: str,
) -> None:
    """同 level 精确唯一约束是幂等 no-op，且不产生第二行。"""
    from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
    from domains.opportunities.service_impl import OpportunityServiceImpl
    from infra.db.session import create_engine_from
    from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork

    class _Scorer:
        async def score(self, *args, **kwargs):
            raise AssertionError("unused")

    class _Auth:
        def require(self, actor, action, scope, tenant_id):
            return "test:allow"

    class _Audit:
        def log(self, **kwargs):
            return None

    HandoffPolicy = importlib.import_module(
        "domains.opportunities.models"
    ).HandoffPolicy
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    tenant = TenantId("tEscService")
    HandoffRepositoryImpl = _load("HandoffRepositoryImpl")
    try:
        seed = AsyncSession(bind=engine, expire_on_commit=False)
        try:
            await _seed_opp(seed, "opp-esc-svc", str(tenant), "need-esc-svc")
            repo = HandoffRepositoryImpl(seed, tenant)
            await repo.add(_handoff("hand-esc-svc", str(tenant), "opp-esc-svc"))
            await seed.commit()
        finally:
            await seed.close()
        service = OpportunityServiceImpl(
            lambda: SqlAlchemyOpportunityUnitOfWork(  # type: ignore[arg-type, return-value]
                factory, tenant
            ),
            _Scorer(),
            HandoffPolicy(sla_seconds=1, backlog_threshold=1),
            authorizer=_Auth(),
            audit=_Audit(),
            now=lambda: _NOW,
        )
        actor = Actor(
            actor_id="system:handoff",
            scope=OpportunityScope(level=ScopeLevel.SYSTEM),
            role="system",
        )
        await service.record_handoff_escalation(
            tenant, HandoffId("hand-esc-svc"), 1, _NOW, actor=actor
        )
        await service.record_handoff_escalation(
            tenant, HandoffId("hand-esc-svc"), 1, _NOW, actor=actor
        )
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM handoff_escalations "
                        "WHERE tenant_id = :tenant AND handoff_id = :handoff AND level = 1"
                    ),
                    {"tenant": str(tenant), "handoff": "hand-esc-svc"},
                )
            ).scalar_one()
        assert count == 1
    finally:
        await engine.dispose()


# --- Batch 2：LossRecord 仓储 ---------------------------------------------------


def _loss(
    loss_record_id: str,
    tenant_id: str,
    opportunity_id: str,
    *,
    reason: str = "price_too_high",
    died: str = "quoted",
    recorded_at=_NOW,
    **overrides,
):
    """构造最小合法归因记录（可覆盖 reason/died/recorded_at 等）。"""
    return LossRecord(
        loss_record_id=LossRecordId(loss_record_id),
        tenant_id=TenantId(tenant_id),
        opportunity_id=OpportunityId(opportunity_id),
        loss_reason=LossReason(reason),
        died_at_state=OpportunityState(died),
        confirmed_by=EmployeeId("emp-1"),
        confirmed_at=_NOW,
        recorded_at=recorded_at,
        **overrides,
    )


async def test_loss_record_add_and_count_2d(repo_session: AsyncSession) -> None:
    """add 只追加；count_by_reason_and_state 按 (loss_reason, died_at_state) 二维 GROUP BY。"""
    LossRecordRepositoryImpl = _load("LossRecordRepositoryImpl")
    await _seed_opp(repo_session, "opp-loss-a", "tLoss1", "need-loss-a")
    repo = LossRecordRepositoryImpl(repo_session, TenantId("tLoss1"))
    await repo.add(
        TenantId("tLoss1"), _loss("loss-a1", "tLoss1", "opp-loss-a", reason="price_too_high", died="quoted")
    )
    await repo.add(
        TenantId("tLoss1"), _loss("loss-a2", "tLoss1", "opp-loss-a", reason="price_too_high", died="quoted")
    )
    await repo.add(
        TenantId("tLoss1"), _loss("loss-a3", "tLoss1", "opp-loss-a", reason="no_reply", died="contacted")
    )
    await repo_session.commit()

    counts = await repo.count_by_reason_and_state(TenantId("tLoss1"), since_days=30)
    assert sorted(counts) == [
        ("no_reply", "contacted", 1),
        ("price_too_high", "quoted", 2),
    ]


async def test_loss_record_count_since_days(repo_session: AsyncSession) -> None:
    """since_days 过滤：窗口外的旧归因不计入。"""
    LossRecordRepositoryImpl = _load("LossRecordRepositoryImpl")
    await _seed_opp(repo_session, "opp-loss-b", "tLoss2", "need-loss-b")
    repo = LossRecordRepositoryImpl(repo_session, TenantId("tLoss2"))
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    recent = now - timedelta(days=1)
    await repo.add(
        TenantId("tLoss2"), _loss("loss-b1", "tLoss2", "opp-loss-b", reason="no_reply", died="contacted", recorded_at=old)
    )
    await repo.add(
        TenantId("tLoss2"), _loss("loss-b2", "tLoss2", "opp-loss-b", reason="price_too_high", died="quoted", recorded_at=recent)
    )
    await repo_session.commit()

    counts = await repo.count_by_reason_and_state(TenantId("tLoss2"), since_days=30)
    assert counts == [("price_too_high", "quoted", 1)]


async def test_loss_record_db_trigger_rejects_update_delete(db_url: str) -> None:
    """DB 只增触发器：loss_records UPDATE/DELETE 直接被拒（仓储无 update/delete 口）。"""
    from infra.db.session import create_engine_from

    LossRecordRepositoryImpl = _load("LossRecordRepositoryImpl")
    OpportunityRepositoryImpl = _load("OpportunityRepositoryImpl")
    engine = create_engine_from(db_url)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        await OpportunityRepositoryImpl(session, TenantId("tTrg")).add(
            _opp("opp-trg-1", "tTrg", "need-trg-1")
        )
        await session.commit()
        await LossRecordRepositoryImpl(session, TenantId("tTrg")).add(
            TenantId("tTrg"), _loss("loss-trg-1", "tTrg", "opp-trg-1")
        )
        await session.commit()

        async def _rejected(sql: str, params: dict[str, object]) -> None:
            async with engine.connect() as conn:
                try:
                    await conn.execute(text(sql), params)
                except DBAPIError:
                    await conn.rollback()
                else:
                    await conn.rollback()
                    pytest.fail(f"应被 DB 只增触发器拒绝：{sql}")

        await _rejected(
            "UPDATE loss_records SET loss_reason = :r WHERE loss_record_id = :id",
            {"r": "no_reply", "id": "loss-trg-1"},
        )
        await _rejected(
            "DELETE FROM loss_records WHERE loss_record_id = :id",
            {"id": "loss-trg-1"},
        )
    finally:
        await session.close()
        await engine.dispose()


async def test_loss_record_tenant_binding_mismatch(repo_session: AsyncSession) -> None:
    """方法租户与绑定不一致：count 空、add 三方租户不一致拒绝。"""
    LossRecordRepositoryImpl = _load("LossRecordRepositoryImpl")
    await _seed_opp(repo_session, "opp-loss-mb", "tLmbA", "need-loss-mb")
    repo_a = LossRecordRepositoryImpl(repo_session, TenantId("tLmbA"))
    await repo_a.add(TenantId("tLmbA"), _loss("loss-mb-1", "tLmbA", "opp-loss-mb"))
    await repo_session.commit()

    assert await repo_a.count_by_reason_and_state(TenantId("tLmbB"), since_days=30) == []

    with pytest.raises(ValueError):
        await repo_a.add(TenantId("tLmbB"), _loss("loss-mb-2", "tLmbB", "opp-loss-mb"))
    with pytest.raises(ValueError):
        await repo_a.add(TenantId("tLmbA"), _loss("loss-mb-3", "tLmbB", "opp-loss-mb"))


# --- Batch 3：FieldProvenance 仓储 -------------------------------------------------


async def test_provenance_roundtrip_full_fields(repo_session: AsyncSession) -> None:
    """shared.Provenance ↔ provenance_records 全字段无损往返（含 WEB_PAGE URL/hash）。"""
    FieldProvenanceRepositoryImpl = _load("FieldProvenanceRepositoryImpl")
    repo = FieldProvenanceRepositoryImpl(repo_session, TenantId("tProv"))
    prov = Provenance(
        source_type=SourceType.WEB_PAGE,
        source_id="page-1",
        extracted_by="model_v3",
        extracted_at=_NOW,
        confirmed_by=EmployeeId("emp-1"),
        confirmed_at=_NOW,
        source_url="https://example.com/company",
        page_hash="abc123",
    )
    await repo.save(TenantId("tProv"), "opportunity", "opp-p1", "account_name", prov)
    await repo_session.commit()

    result = await repo.list_for_entity(TenantId("tProv"), "opportunity", "opp-p1")
    assert result == [("account_name", prov)]


async def test_provenance_same_field_multiple_versions_preserved(
    repo_session: AsyncSession,
) -> None:
    """同字段多版本来源均保留（只增历史），且按 extracted_at 新到旧。"""
    FieldProvenanceRepositoryImpl = _load("FieldProvenanceRepositoryImpl")
    repo = FieldProvenanceRepositoryImpl(repo_session, TenantId("tProv2"))
    earlier = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="m1",
        extracted_by="model_v3",
        extracted_at=_NOW - timedelta(days=1),
    )
    later = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="m2",
        extracted_by="model_v3",
        extracted_at=_NOW,
    )
    await repo.save(TenantId("tProv2"), "opportunity", "opp-p2", "account_name", earlier)
    await repo.save(TenantId("tProv2"), "opportunity", "opp-p2", "account_name", later)
    await repo_session.commit()

    result = await repo.list_for_entity(TenantId("tProv2"), "opportunity", "opp-p2")
    assert result == [("account_name", later), ("account_name", earlier)]


async def test_provenance_entity_type_handoff_customer_verbatim(
    repo_session: AsyncSession,
) -> None:
    """handoff 实体的 customer_verbatim 来源可保存与读取。"""
    FieldProvenanceRepositoryImpl = _load("FieldProvenanceRepositoryImpl")
    repo = FieldProvenanceRepositoryImpl(repo_session, TenantId("tProv3"))
    prov = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="m9",
        extracted_by="model_v3",
        extracted_at=_NOW,
    )
    await repo.save(TenantId("tProv3"), "handoff", "ho-p3", "customer_verbatim", prov)
    await repo_session.commit()

    result = await repo.list_for_entity(TenantId("tProv3"), "handoff", "ho-p3")
    assert result == [("customer_verbatim", prov)]


async def test_provenance_tenant_binding_mismatch(repo_session: AsyncSession) -> None:
    """方法租户与绑定不一致：list 空、save 拒绝。"""
    FieldProvenanceRepositoryImpl = _load("FieldProvenanceRepositoryImpl")
    repo = FieldProvenanceRepositoryImpl(repo_session, TenantId("tPmbA"))
    prov = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="m1",
        extracted_by="model_v3",
        extracted_at=_NOW,
    )
    await repo.save(TenantId("tPmbA"), "opportunity", "opp-pmb", "account_name", prov)
    await repo_session.commit()

    assert (
        await repo.list_for_entity(TenantId("tPmbB"), "opportunity", "opp-pmb") == []
    )
    with pytest.raises(ValueError):
        await repo.save(TenantId("tPmbB"), "opportunity", "opp-pmb", "account_name", prov)


async def test_authentication_request_repository_idempotency_tenant_and_status(
    repo_session: AsyncSession,
) -> None:
    """真实 repository 强制 tenant/key winner、合法状态与终态安全。"""
    repository = importlib.import_module("domains.sending_identity.repository")
    implementation = importlib.import_module(
        "infra.db.repositories.sending_identities"
    )
    required = "AuthenticationCheckRequestRepositoryImpl"
    if not hasattr(implementation, required):
        pytest.fail(f"RED：{required} 尚未实现")
    tenant = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRV")
    other_tenant = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRW")
    identity = "sid_01K2C5R6J7ABCDEFGHJKMNPQRV"
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    await repo_session.execute(
        text(
            "INSERT INTO sending_domains(tenant_id,domain,role,created_at) "
            "VALUES (:tenant,'task4-repo.example.com','cold_outreach',:now)"
        ),
        {"tenant": str(tenant), "now": now},
    )
    await repo_session.execute(
        text(
            "INSERT INTO sending_identities(tenant_id,identity_id,domain,address,state,"
            "throttle_hard_bounce_rate,suspend_hard_bounce_rate,"
            "throttle_complaint_rate,suspend_complaint_rate,suspend_on_spam_trap,"
            "suspend_on_blocklist,minimum_sample,created_at) VALUES "
            "(:tenant,:identity,'task4-repo.example.com','sales@task4-repo.example.com','auth_pending',"
            "0.03,0.05,0.001,0.003,true,true,50,:now)"
        ),
        {"tenant": str(tenant), "identity": identity, "now": now},
    )
    repo = implementation.AuthenticationCheckRequestRepositoryImpl(
        repo_session, tenant
    )
    view = repository.AuthenticationCheckRequestView(
        importlib.import_module(
            "shared.schemas.identifiers"
        ).AuthenticationCheckRequestId("acr_01K2C5R6J7ABCDEFGHJKMNPQRV"),
        tenant,
        importlib.import_module(
            "shared.schemas.identifiers"
        ).SendingIdentityId(identity),
        importlib.import_module("shared.schemas.identifiers").IdempotencyKey(
            "repo-request-key"
        ),
        repository.AuthenticationCheckRequestStatus.REQUESTED,
        now,
        None,
    )
    created = await repo.create_or_get(view)
    duplicate = await repo.create_or_get(view)
    assert created.created is True
    assert duplicate.created is False
    assert duplicate.winner == view
    assert await repo.get(other_tenant, view.request_id) is None
    running = await repo.transition(
        tenant,
        view.request_id,
        repository.AuthenticationCheckRequestStatus.RUNNING,
        None,
    )
    assert running.status is repository.AuthenticationCheckRequestStatus.RUNNING
    succeeded = await repo.transition(
        tenant,
        view.request_id,
        repository.AuthenticationCheckRequestStatus.SUCCEEDED,
        now,
    )
    assert succeeded.completed_at == now
    with pytest.raises(InvalidStateTransition):
        await repo.transition(
            tenant,
            view.request_id,
            repository.AuthenticationCheckRequestStatus.RUNNING,
            None,
        )


def test_feedback_receipt_kind_and_target_constraints_include_complaint() -> None:
    """ORM 词表必须与 0017 迁移一致：complaint 进入 kind 与 target 约束。"""
    from sqlalchemy import CheckConstraint

    metadata = importlib.import_module("infra.db.tables").Base.metadata
    table = metadata.tables["email_feedback_receipts"]
    texts = {
        str(constraint.name): str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }
    assert "complaint" in texts["ck_email_feedback_receipt_kind"]
    assert "complaint" in texts["ck_email_feedback_receipt_target"]
