"""OpportunityScorer 实现：策略注入 + 快照持久化。

未过硬门槛**仍然存失败快照**（哨兵 ``FAILED_GATE_SENTINEL``）——被拦在哪个门槛、
拦在哪一条，是调整门槛的唯一依据。通过才调 ``compute_score``（绝不拿未过门槛的
输入走打分路径）。快照只增（只调 ``add``，无改写入口）。
"""
from __future__ import annotations

from datetime import UTC, datetime

from domains.opportunities.models import ScoreSnapshot, SortKey
from domains.opportunities.repository import ScoreSnapshotRepository
from domains.opportunities.scoring import (
    ScoringInput,
    ScoringPolicy,
    check_gates,
    compute_score,
    rank_bucket,
)
from shared.schemas.identifiers import OpportunityId, ScoreSnapshotId, TenantId, new_id

FAILED_GATE_SENTINEL = SortKey(0, 0, 0)
"""系统保留哨兵（失败门槛快照的排序键）。非概率、非业务权重——0 只表示「未打分」。"""


class OpportunityScorerImpl:
    """``OpportunityScorer`` 的默认实现：注入 ``ScoringPolicy``（无默认业务数字）。"""

    def __init__(self, policy: ScoringPolicy) -> None:
        self._policy = policy

    async def score(
        self,
        snapshots: ScoreSnapshotRepository,
        tenant_id: TenantId,
        opportunity_id: str,
        input: ScoringInput,
    ) -> ScoreSnapshot:
        """先 ``check_gates``；未过存失败快照（哨兵），通过则 compute_score+rank_bucket。

        快照带完整原始输入（evidence/value/supply）、版本、门槛结果与归因，
        供 Phase 2 回测；``scored_at`` 用 UTC aware 当前时间。
        """
        result = check_gates(input)
        if result.all_passed:
            sort_key = compute_score(input, self._policy)
            bucket = rank_bucket(sort_key.evidence_rank, self._policy)
        else:
            sort_key = FAILED_GATE_SENTINEL
            bucket = "low"

        snapshot = ScoreSnapshot(
            tenant_id=tenant_id,
            snapshot_id=ScoreSnapshotId(new_id("snap")),
            opportunity_id=OpportunityId(opportunity_id),
            scored_at=datetime.now(UTC),
            scorer_version=self._policy.version,
            passed_gates=[gate.value for gate in result.passed],
            failed_gates=[gate.value for gate in result.failed],
            evidence_tier=input.evidence_tier,
            estimated_value=input.estimated_order_value,
            supply_available=input.supply_available,
            sort_key=sort_key,
            rank_bucket=bucket,
            gate_reasons=result.reasons,
        )
        await snapshots.add(tenant_id, snapshot)
        return snapshot
