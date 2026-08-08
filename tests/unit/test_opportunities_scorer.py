"""S2-9 OpportunityScorer 单测（策略注入 + 快照持久化，fake repo）。

行为断言，不依赖实现细节：
- 未过硬门槛：仍存失败快照——哨兵 ``SortKey(0,0,0)``（系统保留，非概率/业务权重）、
  rank_bucket=low、passed/failed/gate_reasons 全量、tenant/snapshot_id/opportunity_id/
  scorer_version=policy.version、原始 evidence/value/supply 输入完整。
- 通过：``compute_score`` + ``rank_bucket`` 存快照；**未过门槛绝不调用 compute_score**。
- fake repo：断言 add(tenant_id, snapshot) 的 tenant 一致、返回对象就是保存对象、
  无 update/改写入口、repo 异常不吞；多次 score 生成不同 snapshot_id、旧快照不修改。
- scored_at 必须 UTC aware 且在合理时间窗口内（不硬编码时刻）。

RED：``domains/opportunities/scorer`` 尚未创建；经 importlib 延迟导入转行为失败。
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.opportunities.scoring import ScoringInput, ScoringPolicy, SortKey
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import OpportunityId, TenantId
from shared.schemas.money import CurrencyCode, Money

_USD = CurrencyCode("USD")
_100 = Money(Decimal(100), _USD)

_MODULE_BY_SYMBOL = {
    "OpportunityScorerImpl": "domains.opportunities.scorer",
}


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED 阶段 scorer 未建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _policy() -> ScoringPolicy:
    return ScoringPolicy(
        version="gates-v1",
        value_band_boundaries=(_100, Money(Decimal(1000), _USD)),
        bucket_map={i: "low" for i in range(1, 6)} | {6: "mid", 7: "high"},
    )


class _FakeSnapshotRepo:
    """只记录 add 的 fake；读取接口抛 AssertionError（scorer 不得调用）。"""

    def __init__(self, *, raise_on_add: Exception | None = None) -> None:
        self.added: list[tuple[object, object]] = []
        self.raise_on_add = raise_on_add

    async def add(self, tenant_id: object, snapshot: object) -> None:
        if self.raise_on_add is not None:
            raise self.raise_on_add
        self.added.append((tenant_id, snapshot))

    async def latest_for_opportunity(self, tenant_id: object, opportunity_id: object) -> None:
        raise AssertionError("scorer 不得调用读取接口")

    async def list_for_backtest(self, tenant_id: object, since_days: int) -> None:
        raise AssertionError("scorer 不得调用读取接口")


def _fail_all_input() -> ScoringInput:
    return ScoringInput(
        has_verified_contact=False,
        evidence_tier=ConfidenceTier.LOW,
        category_allowed=False,
        minimum_order_value=_100,
        estimated_order_value=None,
    )


def _pass_input() -> ScoringInput:
    return ScoringInput(
        has_verified_contact=True,
        evidence_tier=ConfidenceTier.HIGH,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(1500), _USD),
        supply_available=True,
    )


async def test_failed_gates_store_snapshot() -> None:
    """未过门槛：存失败快照（哨兵 SortKey(0,0,0)、failed 全、bucket=low、输入完整）。"""
    OpportunityScorerImpl = _load("OpportunityScorerImpl")
    scorer = OpportunityScorerImpl(_policy())
    repo = _FakeSnapshotRepo()
    snapshot = await scorer.score(repo, TenantId("t1"), "opp1", _fail_all_input())

    tenant_arg, saved_arg = repo.added[0]
    assert tenant_arg == TenantId("t1")
    assert snapshot.sort_key == SortKey(0, 0, 0)  # 系统保留哨兵，非业务权重
    assert snapshot.rank_bucket == "low"
    assert snapshot.tenant_id == TenantId("t1")
    assert snapshot.opportunity_id == OpportunityId("opp1")
    assert snapshot.scorer_version == "gates-v1"
    assert set(snapshot.failed_gates) == {
        "contactable",
        "evidence_sufficient",
        "value_above_floor",
        "category_allowed",
    }
    assert snapshot.passed_gates == []
    assert snapshot.gate_reasons  # 全量 reasons
    assert snapshot.evidence_tier == ConfidenceTier.LOW
    assert snapshot.estimated_value is None
    assert snapshot.supply_available is None
    assert saved_arg is snapshot  # 返回对象就是保存对象（identity，最后断言避免 mypy 收窄）


async def test_passed_gates_store_sortkey() -> None:
    """通过：compute_score + rank_bucket 存快照。"""
    OpportunityScorerImpl = _load("OpportunityScorerImpl")
    scorer = OpportunityScorerImpl(_policy())
    repo = _FakeSnapshotRepo()
    snapshot = await scorer.score(repo, TenantId("t1"), "opp1", _pass_input())

    assert snapshot.sort_key == SortKey(5, 2, 2)  # HIGH=5、越 100/1000 两边界、True→2
    assert snapshot.rank_bucket == _policy().bucket_map[5]
    assert snapshot.failed_gates == []
    assert snapshot.estimated_value == Money(Decimal(1500), _USD)
    assert snapshot.supply_available is True


async def test_snapshot_stores_gate_reasons() -> None:
    """GateResult.reasons 全量落入 snapshot.gate_reasons；通过的门槛无 reason。"""
    OpportunityScorerImpl = _load("OpportunityScorerImpl")
    scorer = OpportunityScorerImpl(_policy())
    repo = _FakeSnapshotRepo()
    input = ScoringInput(
        has_verified_contact=False,
        evidence_tier=ConfidenceTier.LOW,
        category_allowed=True,
        minimum_order_value=_100,
        estimated_order_value=Money(Decimal(50), _USD),  # 低于底线
    )
    snapshot = await scorer.score(repo, TenantId("t1"), "opp1", input)

    assert snapshot.gate_reasons["contactable"]
    assert snapshot.gate_reasons["evidence_sufficient"]
    assert snapshot.gate_reasons["value_above_floor"]
    assert "category_allowed" not in snapshot.gate_reasons
    assert snapshot.passed_gates == ["category_allowed"]


async def test_failed_gates_do_not_call_compute_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """未过门槛绝不调用 compute_score（哨兵直接入库）。"""
    OpportunityScorerImpl = _load("OpportunityScorerImpl")
    scorer = OpportunityScorerImpl(_policy())
    repo = _FakeSnapshotRepo()

    def _boom(*args, **kwargs):
        raise AssertionError("compute_score 不应被调用")

    monkeypatch.setattr("domains.opportunities.scorer.compute_score", _boom)
    snapshot = await scorer.score(repo, TenantId("t1"), "opp1", _fail_all_input())
    assert snapshot.sort_key == SortKey(0, 0, 0)


async def test_snapshot_immutable_no_update_no_rewrite() -> None:
    """多次 score 生成不同 snapshot_id；fake 无 update 改写入口；旧快照不修改。"""
    OpportunityScorerImpl = _load("OpportunityScorerImpl")
    scorer = OpportunityScorerImpl(_policy())
    repo = _FakeSnapshotRepo()
    s1 = await scorer.score(repo, TenantId("t1"), "opp1", _pass_input())
    s2 = await scorer.score(repo, TenantId("t1"), "opp1", _pass_input())

    assert s1.snapshot_id != s2.snapshot_id
    assert s1.sort_key == s2.sort_key  # 旧快照未被改写
    assert len(repo.added) == 2
    assert {t for t, _ in repo.added} == {TenantId("t1")}  # 两次 add 的 tenant 均一致
    assert not hasattr(repo, "update")  # 仓储无 update/改写入口


async def test_scorer_repo_exception_not_swallowed() -> None:
    """repo.add 异常不吞：原样抛出。"""
    OpportunityScorerImpl = _load("OpportunityScorerImpl")
    scorer = OpportunityScorerImpl(_policy())
    repo = _FakeSnapshotRepo(raise_on_add=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await scorer.score(repo, TenantId("t1"), "opp1", _pass_input())


async def test_scorer_time_is_utc_aware() -> None:
    """scored_at 必须 UTC aware 且在合理时间窗口内（不硬编码时刻）。"""
    OpportunityScorerImpl = _load("OpportunityScorerImpl")
    scorer = OpportunityScorerImpl(_policy())
    repo = _FakeSnapshotRepo()
    before = datetime.now(UTC)
    snapshot = await scorer.score(repo, TenantId("t1"), "opp1", _pass_input())
    after = datetime.now(UTC)

    assert snapshot.scored_at.tzinfo is not None
    assert snapshot.scored_at.utcoffset() == timedelta(0)  # UTC
    assert before <= snapshot.scored_at <= after
