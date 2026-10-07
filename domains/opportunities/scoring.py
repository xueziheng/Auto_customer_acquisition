"""机会打分：硬门槛 + 三因子。

设计稿第八节给了九项权重。**Phase 1 不用**，理由见
``docs/architecture/09-scoring-and-feedback.md``：权重没有数据支撑，
九因子也无法 debug——销售问「为什么这个是 67 分」时答不上来，
这个分数就不会被信任，那它就没有价值。

Phase 1 的设计目标不是「算得准」，而是**算得可解释、且输入可回测**。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import pairwise
from typing import Protocol, runtime_checkable

from domains.opportunities.models import ScoreSnapshot, SortKey
from domains.opportunities.repository import ScoreSnapshotRepository
from shared.errors import CurrencyMismatchError, ValidationError
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import TenantId
from shared.schemas.money import Money

SCORER_VERSION = "gates-v1"
"""打分器版本。每次改门槛或因子都要升版本并记进快照——
否则回测时无法区分「策略变了」和「市场变了」。"""


class Gate(str, Enum):
    """硬门槛。任一不通过直接淘汰，**不进入打分**。

    为什么用门槛而不是扣分：核心业务公式是乘法
    （可接触 × 真实 × 可供应 × 有利润 × 有人执行），任一项为零则
    整体为零。用加权扣分会让「联系不上但预计金额很大」的机会仍然
    排在前面，那是错的——联系不上就是零。
    """

    CONTACTABLE = "contactable"
    """有至少一个已验证的联系方式（硬边界 6）。

    未验证的联系方式不算——它进不了序列，等于联系不上。"""

    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    """证据等级达标。

    纯 ``AGENT_INDUSTRY_INFERENCE`` 推出来的假设不够格进触达队列：
    「家具厂通常需要五金件」对任何一家家具厂都成立，这种推断没有
    区分度，按它触达等于随机群发。"""

    VALUE_ABOVE_FLOOR = "value_above_floor"
    """预计订单额过 Playbook 里的底线。

    小单不是不能做，但不该占用自动化探索的预算。"""

    CATEGORY_ALLOWED = "category_allowed"
    """不在禁售或高风险清单里。

    读 Company Playbook 的排除清单。这道门槛越早过越好——
    在打分阶段拦下来，比在寻源阶段发现做不了省得多。"""


MINIMUM_EVIDENCE_TIER = ConfidenceTier.LOW_MID
"""进入触达的最低证据档位。

``LOW`` 档基本只有行业推断，区分度不够。设成 ``LOW_MID``
（至少有一条公开企业变化信号）是刻意的保守选择：
宁可少联系，不要把预算花在随机推断上。
"""

# 显式稳定档位序号（LOW=1 … EXTREME=7），与 ConfidenceTier 七枚举一一对应。
# 只映射档位→序号，不复刻 shared 的 derive_confidence 推导规则。
_TIER_RANK: dict[ConfidenceTier, int] = {
    ConfidenceTier.LOW: 1,
    ConfidenceTier.LOW_MID: 2,
    ConfidenceTier.MID: 3,
    ConfidenceTier.MID_HIGH: 4,
    ConfidenceTier.HIGH: 5,
    ConfidenceTier.VERY_HIGH: 6,
    ConfidenceTier.EXTREME: 7,
}

# supply_available → sort_key.supply_rank（False/None/True → 0/1/2）。
_SUPPLY_RANK: dict[bool | None, int] = {False: 0, None: 1, True: 2}


@dataclass(frozen=True)
class GateResult:
    """门槛检查结果。

    字段：
        passed:  通过的门槛
        failed:  未通过的门槛
        reasons: 每个未通过门槛的具体原因

    ``reasons`` 必须具体：写「联系方式 xxx@ 验证状态为 risky」，
    不要写「联系方式不合格」。Agent 会读这个结果决定下一步
    （是补数据还是放弃）。
    """

    passed: list[Gate]
    failed: list[Gate]
    reasons: dict[str, str]

    @property
    def all_passed(self) -> bool:
        return not self.failed


@dataclass(frozen=True)
class ScoringInput:
    """打分输入。

    刻意做成扁平的值对象，**不接收领域实体**——这样打分逻辑是纯函数，
    可以直接喂测试样本，不用构造整个对象图。

    字段：
        has_verified_contact
        evidence_tier
        estimated_order_value
        category_allowed
        supply_available:      供应是否可得（None = 未查）
        minimum_order_value:   Playbook 底线
        is_repeat_buyer_likely
    """

    has_verified_contact: bool
    evidence_tier: ConfidenceTier
    category_allowed: bool
    minimum_order_value: Money
    estimated_order_value: Money | None = None
    supply_available: bool | None = None
    is_repeat_buyer_likely: bool = False


def check_gates(input: ScoringInput) -> GateResult:
    """检查硬门槛。纯函数，无 IO。

    实现要求：
    - **检查全部门槛，不要短路返回。** 一次告诉调用方所有问题，
      否则 Agent 要试四轮才知道要补多少东西。
    - ``estimated_order_value`` 为 None 时 ``VALUE_ABOVE_FLOOR``
      算未通过，原因写「预计金额未知」——不要乐观地放过去。
    - estimated 与 minimum 币种不一致抛 ``CurrencyMismatchError``（无法比较）。
    """
    passed: list[Gate] = []
    failed: list[Gate] = []
    reasons: dict[str, str] = {}

    if input.has_verified_contact:
        passed.append(Gate.CONTACTABLE)
    else:
        failed.append(Gate.CONTACTABLE)
        reasons[Gate.CONTACTABLE.value] = "没有已验证的联系方式（硬边界 6）"

    if _TIER_RANK[input.evidence_tier] < _TIER_RANK[MINIMUM_EVIDENCE_TIER]:
        failed.append(Gate.EVIDENCE_SUFFICIENT)
        reasons[Gate.EVIDENCE_SUFFICIENT.value] = (
            f"证据档位 {input.evidence_tier.value} 低于最低档 {MINIMUM_EVIDENCE_TIER.value}"
        )
    else:
        passed.append(Gate.EVIDENCE_SUFFICIENT)

    if input.category_allowed:
        passed.append(Gate.CATEGORY_ALLOWED)
    else:
        failed.append(Gate.CATEGORY_ALLOWED)
        reasons[Gate.CATEGORY_ALLOWED.value] = "品类在禁售或高风险清单中"

    estimated = input.estimated_order_value
    minimum = input.minimum_order_value
    if estimated is None:
        failed.append(Gate.VALUE_ABOVE_FLOOR)
        reasons[Gate.VALUE_ABOVE_FLOOR.value] = "预计金额未知"
    elif estimated.currency != minimum.currency:
        raise CurrencyMismatchError(
            f"预计金额币种 {estimated.currency} 与 Playbook 底线币种 {minimum.currency} 不一致",
            context={
                "estimated_currency": estimated.currency,
                "minimum_currency": minimum.currency,
            },
        )
    elif estimated.amount < minimum.amount:
        failed.append(Gate.VALUE_ABOVE_FLOOR)
        reasons[Gate.VALUE_ABOVE_FLOOR.value] = (
            f"预计金额 {estimated.amount} 低于底线 {minimum.amount}"
        )
    else:
        passed.append(Gate.VALUE_ABOVE_FLOOR)

    return GateResult(passed=passed, failed=failed, reasons=reasons)


@dataclass(frozen=True)
class ScoringPolicy:
    """打分策略（版本化，**上层注入，无默认业务数字**）。

    - ``value_band_boundaries``：升序 Money 上界；value_band = 被越过的边界数。
      非空 / 严格升序 / 同币种。
    - ``bucket_map``：evidence_rank（1..7）→ ``'high'``/``'mid'``/``'low'``，
      必须覆盖全部 7 档。
    """

    version: str
    value_band_boundaries: tuple[Money, ...]
    bucket_map: dict[int, str]

    def __post_init__(self) -> None:
        if not self.value_band_boundaries:
            raise ValidationError("value_band_boundaries 不能为空")
        boundaries = self.value_band_boundaries
        for lower, upper in pairwise(boundaries):
            if lower.amount >= upper.amount:
                raise ValidationError("value_band_boundaries 必须严格升序")
            if lower.currency != upper.currency:
                raise ValidationError("value_band_boundaries 必须同币种")
        if set(self.bucket_map) != set(range(1, 8)):
            raise ValidationError("bucket_map 必须覆盖 evidence_rank 1..7")
        if not set(self.bucket_map.values()).issubset({"high", "mid", "low"}):
            raise ValidationError("bucket_map 值必须为 high/mid/low")


def compute_score(input: ScoringInput, policy: ScoringPolicy) -> SortKey:
    """三因子排序键（``SortKey`` 字典序，09 文档：先证据、再价值、再供应）。

    实现要求（S2-8 落地）：
    - 纯函数，无 IO；绝不加权/对数/float——证据主导的 tuple 比较
    - ``ScoringPolicy.value_band_boundaries`` 划分 value_band；
      estimated value 币种不匹配抛 ``CurrencyMismatchError``
    - 排序键存进快照供回测；加新因子在此调整，调用方不改
    """
    evidence_rank = _TIER_RANK[input.evidence_tier]
    estimated = input.estimated_order_value
    if estimated is None:
        value_band = 0
    else:
        boundaries = policy.value_band_boundaries
        if estimated.currency != boundaries[0].currency:
            raise CurrencyMismatchError(
                f"预计金额币种 {estimated.currency} 与价值带边界币种 "
                f"{boundaries[0].currency} 不一致",
                context={
                    "estimated_currency": estimated.currency,
                    "boundary_currency": boundaries[0].currency,
                },
            )
        # value_band = 被严格越过的边界数：value 必须**大于**边界才计（等于不算）。
        value_band = sum(1 for b in boundaries if b.amount < estimated.amount)
    supply_rank = _SUPPLY_RANK[input.supply_available]
    return SortKey(evidence_rank, value_band, supply_rank)


def rank_bucket(evidence_rank: int, policy: ScoringPolicy) -> str:
    """分桶：只由 ``evidence_rank`` 查注入 policy 的 bucket_map，不临场算档位。"""
    return policy.bucket_map[evidence_rank]


@runtime_checkable
class OpportunityScorer(Protocol):
    """打分服务。"""

    async def score(
        self,
        snapshots: ScoreSnapshotRepository,
        tenant_id: TenantId,
        opportunity_id: str,
        input: ScoringInput,
    ) -> ScoreSnapshot:
        """打分并**持久化快照**（snapshot repo 由 UoW 提供，绑定同事务 session）。

        实现要求：
        - 先 ``check_gates``；未通过则失败哨兵 ``sort_key = SortKey(0, 0, 0)``、
          ``failed_gates`` 记全、``rank_bucket='low'``，仍然存快照。
          存失败的快照和存成功的一样重要——「哪些机会被门槛拦了、
          拦在哪一条」是调整门槛的唯一依据。
        - 全部通过则 ``compute_score(input, policy)`` 得 ``SortKey`` 并分桶，存快照
        - 快照里记 ``scorer_version``（来自注入的 ``ScoringPolicy.version``）
        - 排序键是字典序三元（证据主导），绝不加权/对数/float；
          分桶给界面用：销售看「高/中/低」比看 67.3 更有用
        """
        ...
