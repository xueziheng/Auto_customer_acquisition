"""证据等级与置信度推导。

硬边界 3 的落地实现，决策背景见 ``docs/adr/0005-*.md``。

核心原则：**模型只判断"某条证据属于哪个等级"，置信度由本模块的
确定性代码推导。数据库里没有 confidence 数值列。**

为什么：大模型输出的 ``0.67`` 没有校准——它不代表"67% 可能为真"，
只是模型在那个 token 位置倾向输出的数字，换个问法就会变。存下来会
诱发两种误用：拿它比较大小（0.67 比 0.61 更值得做？），拿它算加权
平均。两种操作在数字未校准的前提下都无意义，但看起来很像数据驱动。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from shared.errors import ValidationError


class EvidenceLevel(str, Enum):
    """证据等级。由模型判断某条证据属于哪一档——这是对事实的**分类**，
    不是概率估计，所以模型可以做，而且换模型后结果应保持一致。

    顺序即强度，从弱到强。
    """

    AGENT_INDUSTRY_INFERENCE = "agent_industry_inference"
    """Agent 从行业常识推断。例："家具制造商通常需要五金件。"
    最弱：没有关于这家企业的任何具体观察。"""

    PUBLIC_COMPANY_EVENT = "public_company_event"
    """公开的企业变化信号。例：工厂扩建公告、新增产品线、招聘采购岗。
    有具体观察，但采购需求仍是推断。"""

    EMPLOYEE_GUESS = "employee_guess"
    """员工凭经验判断可能有需求。比公开信号强，因为员工可能有未记录的
    背景信息；但仍是推测，必须标注为推断而非事实。"""

    CUSTOMER_INTEREST_REPLY = "customer_interest_reply"
    """客户回复表示有兴趣，但未给具体信息。第一次从客户本人处获得信号。"""

    CUSTOMER_SPECIFICATION = "customer_specification"
    """客户明确给出规格（材质、尺寸、型号）。已经是事实而非推断。"""

    CUSTOMER_QUANTITY_AND_TIMING = "customer_quantity_and_timing"
    """客户给出数量、时间和目的地。达到可寻源的信息量。"""

    CUSTOMER_SAMPLE_OR_QUOTE_REQUEST = "customer_sample_or_quote_request"
    """客户主动请求样品或正式报价。最强信号：客户在投入自己的时间。"""


class ConfidenceTier(str, Enum):
    """置信度档位。**离散值，不是小数。**

    由 ``derive_confidence`` 推导，不接受外部直接赋值。
    """

    LOW = "low"
    LOW_MID = "low_mid"
    MID = "mid"
    MID_HIGH = "mid_high"
    HIGH = "high"
    VERY_HIGH = "very_high"
    EXTREME = "extreme"


@dataclass(frozen=True)
class EvidenceItem:
    """一条证据。

    字段：
        level:       证据等级（模型判断）
        source_type: 来源类型，见 ``provenance.SourceType``
        source_id:   来源标识（消息 ID、页面哈希、上传 ID）
        observed_at: 观察时间，用于新鲜度判断
        summary:     一句话说明这条证据是什么
    """

    level: EvidenceLevel
    source_type: str
    source_id: str
    observed_at: datetime
    summary: str


@dataclass(frozen=True)
class ConfidenceResult:
    """推导结果。

    字段：
        tier:          档位
        has_conflict:  是否存在相互矛盾的证据
        is_stale:      是否全部证据都超出新鲜度窗口
        explanation:   人类可读的推导说明，必须能回答"为什么是这一档"
        applied_rules: 命中了哪些推导规则，便于回溯和单测
    """

    tier: ConfidenceTier
    has_conflict: bool
    is_stale: bool
    explanation: str
    applied_rules: list[str]


DEFAULT_FRESHNESS_WINDOW = timedelta(days=7)
"""默认新鲜度窗口。可按 Skill manifest 覆盖——供应商价格的有效期
比企业扩建公告短得多。"""

# 显式等级 → 档位映射（05-data-plane 表 + ADR 0005）。
_LEVEL_TO_TIER: dict[EvidenceLevel, ConfidenceTier] = {
    EvidenceLevel.AGENT_INDUSTRY_INFERENCE: ConfidenceTier.LOW,
    EvidenceLevel.PUBLIC_COMPANY_EVENT: ConfidenceTier.LOW_MID,
    EvidenceLevel.EMPLOYEE_GUESS: ConfidenceTier.MID,
    EvidenceLevel.CUSTOMER_INTEREST_REPLY: ConfidenceTier.MID_HIGH,
    EvidenceLevel.CUSTOMER_SPECIFICATION: ConfidenceTier.HIGH,
    EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING: ConfidenceTier.VERY_HIGH,
    EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST: ConfidenceTier.EXTREME,
}

# 显式稳定档位顺序（弱 → 强），meets_threshold 据此比较。
_TIER_ORDER: list[ConfidenceTier] = [
    ConfidenceTier.LOW,
    ConfidenceTier.LOW_MID,
    ConfidenceTier.MID,
    ConfidenceTier.MID_HIGH,
    ConfidenceTier.HIGH,
    ConfidenceTier.VERY_HIGH,
    ConfidenceTier.EXTREME,
]
_TIER_RANK: dict[ConfidenceTier, int] = {tier: i for i, tier in enumerate(_TIER_ORDER)}

# 固定规则名（计划 P8）；applied_rules 按推导应用顺序输出。
_RULE_BASE = "base_from_highest"
_RULE_BOOST = "independence_boost"
_RULE_CONFLICT = "conflict_penalty"
_RULE_STALE = "staleness_penalty"
_RULE_CLAMP = "clamp"


def derive_confidence(
    evidence: list[EvidenceItem],
    *,
    now: datetime,
    freshness_window: timedelta = DEFAULT_FRESHNESS_WINDOW,
    conflicting_pairs: list[tuple[str, str]] | None = None,
) -> ConfidenceResult:
    """依据证据列表推导置信度档位。

    推导规则（按顺序应用，每条都要有单测）：

    1. 取所有证据中的**最高等级**作为基准档。
    2. 同等级存在多条**独立**证据可上浮一档。
       独立 = ``source_id`` 不同**且**来源不是同一页面/同一条消息。
       同一页面抓两次不算两条证据。
    3. 存在相互矛盾的证据则下浮一档，并置 ``has_conflict=True``。
       矛盾对由调用方通过 ``conflicting_pairs`` 传入——判断两条证据
       是否矛盾需要业务语义，不在本模块决定。
    4. 全部证据都超出 ``freshness_window`` 则下浮一档，置 ``is_stale=True``。
    5. 档位不越界：上浮不超过 ``EXTREME``，下浮不低于 ``LOW``。

    实现要求：
    - **纯函数，无 IO。** 时间由 ``now`` 传入，不在函数内取当前时间，
      否则无法测试。
    - ``explanation`` 必须具体：写"唯一证据是一条公开的工厂扩建公告，
      属中低档"，不要写"综合评估为中低"。老板要能看懂并追问。
    - 空证据列表抛错，不返回 ``LOW``——"没有证据"和"证据很弱"是
      不同的情况，前者说明调用方有 bug。
    """
    if not evidence:
        raise ValidationError("证据列表不能为空：没有证据和证据很弱是两回事")
    if freshness_window <= timedelta(0):
        raise ValidationError("freshness_window 必须为正")

    # 规则 1：基准档 = 最高证据等级对应的档位（按显式映射取最大档位）。
    # 直接保留最高等级层的实际证据项，据此取基准等级/档位，不引入反向映射。
    max_rank = max(_TIER_RANK[_LEVEL_TO_TIER[item.level]] for item in evidence)
    highest_items = [
        item for item in evidence if _TIER_RANK[_LEVEL_TO_TIER[item.level]] == max_rank
    ]
    base_tier = _TIER_ORDER[max_rank]
    base_level = highest_items[0].level
    applied_rules: list[str] = [_RULE_BASE]

    # 规则 2：独立上浮——只看最高等级层是否 >=2 条不同 source_id。
    highest_sources = {item.source_id for item in highest_items}
    raw_rank = max_rank
    out_of_bounds = False
    if len(highest_sources) >= 2:
        raw_rank += 1
        out_of_bounds = raw_rank > _TIER_RANK[ConfidenceTier.EXTREME]
        applied_rules.append(_RULE_BOOST)

    # 规则 3：矛盾下浮——pair 两端都在证据 source_id 集合才算，多条只罚一次。
    source_ids = {item.source_id for item in evidence}
    has_conflict = False
    for a, b in (conflicting_pairs or []):
        if a in source_ids and b in source_ids:
            has_conflict = True
            break
    if has_conflict:
        raw_rank -= 1
        out_of_bounds = out_of_bounds or raw_rank < 0
        applied_rules.append(_RULE_CONFLICT)

    # 规则 4：过期下浮——仅当全部证据严格早于 now - window。
    is_stale = all(item.observed_at < now - freshness_window for item in evidence)
    if is_stale:
        raw_rank -= 1
        out_of_bounds = out_of_bounds or raw_rank < 0
        applied_rules.append(_RULE_STALE)

    # 规则 5：最终钳制（不提前钳制中间值；若中间曾越界或最终越界则记录 clamp）。
    final_rank = max(0, min(raw_rank, _TIER_RANK[ConfidenceTier.EXTREME]))
    if out_of_bounds or final_rank != raw_rank:
        applied_rules.append(_RULE_CLAMP)
    tier = _TIER_ORDER[final_rank]

    # explanation 需列出最高等级层的非重复证据摘要，让老板看到"依据是什么"。
    highest_summaries = _unique_nonblank_in_order([item.summary for item in highest_items])
    explanation = _build_explanation(base_level, base_tier, tier, applied_rules, highest_summaries)
    return ConfidenceResult(
        tier=tier,
        has_conflict=has_conflict,
        is_stale=is_stale,
        explanation=explanation,
        applied_rules=applied_rules,
    )


def _unique_nonblank_in_order(summaries: list[str]) -> list[str]:
    """保持输入顺序的去重，并过滤 strip 后为空的摘要。

    不能用 set 直接生成输出顺序——哈希序会破坏纯函数的确定性。
    """
    seen: set[str] = set()
    result: list[str] = []
    for summary in summaries:
        stripped = summary.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        result.append(stripped)
    return result


def _build_explanation(
    base_level: EvidenceLevel,
    base_tier: ConfidenceTier,
    final_tier: ConfidenceTier,
    applied_rules: list[str],
    highest_summaries: list[str],
) -> str:
    """中文具体说明：含基准等级、基准档位、最高等级证据摘要、命中规则名；
    不输出概率数字。"""
    parts = [f"证据最高等级为 {base_level.value}，基准档位 {base_tier.value}（{_RULE_BASE}）"]
    if highest_summaries:
        parts.append("依据的最高等级证据：" + "；".join(highest_summaries))
    if _RULE_BOOST in applied_rules:
        parts.append("最高等级层有多条独立证据，上浮一档（independence_boost）")
    if _RULE_CONFLICT in applied_rules:
        parts.append("存在相互矛盾的证据，下浮一档（conflict_penalty）")
    if _RULE_STALE in applied_rules:
        parts.append("全部证据超出新鲜度窗口，下浮一档（staleness_penalty）")
    if _RULE_CLAMP in applied_rules:
        parts.append("档位曾越界，钳制到合法范围（clamp）")
    parts.append(f"最终档位 {final_tier.value}")
    return "；".join(parts) + "。"


def meets_threshold(result: ConfidenceResult, minimum: ConfidenceTier) -> bool:
    """判断是否达到最低档位要求。

    用于打分硬门槛：纯 ``AGENT_INDUSTRY_INFERENCE`` 推出来的假设不够格
    进入触达队列，见 ``docs/architecture/09-scoring-and-feedback.md``。
    """
    return _TIER_RANK[result.tier] >= _TIER_RANK[minimum]
