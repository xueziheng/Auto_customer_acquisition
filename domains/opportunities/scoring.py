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
from typing import Protocol, runtime_checkable

from domains.opportunities.models import ScoreSnapshot
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
        raise NotImplementedError


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
    """
    raise NotImplementedError


def compute_score(input: ScoringInput) -> tuple[float, dict[str, float]]:
    """三因子打分，返回总分与各因子得分。

    仅用于**排序**，不用于「够不够格」——那是门槛的事。

    三个因子：
    - ``evidence_strength``  证据档位映射成分数
    - ``estimated_value``    预计订单额（对数压缩，避免一个大单
                             把其他机会全压到底部）
    - ``supply_availability`` 供应可得性（None 记为中间值，
                             不是 0——未查不等于查不到）

    实现要求：
    - 纯函数，无 IO
    - ``factor_scores`` 必须返回，存进快照供回测
    - 加新因子时**在这里加**，调用方不改。这是接口设计的目的：
      Phase 2 加因子不该引起连锁修改。
    """
    raise NotImplementedError


@runtime_checkable
class OpportunityScorer(Protocol):
    """打分服务。"""

    async def score(
        self, tenant_id: TenantId, opportunity_id: str, input: ScoringInput
    ) -> ScoreSnapshot:
        """打分并**持久化快照**。

        实现要求：
        - 先 ``check_gates``；未通过则 ``total_score`` 记 0、
          ``failed_gates`` 记全，仍然存快照。
          存失败的快照和存成功的一样重要——「哪些机会被门槛拦了、
          拦在哪一条」是调整门槛的唯一依据。
        - 全部通过则 ``compute_score`` 并存快照
        - 快照里记 ``SCORER_VERSION``
        - 分桶（``rank_bucket``）给界面用：销售看「高/中/低」比看
          67.3 分更有用，也更不容易被过度解读
        """
        ...
