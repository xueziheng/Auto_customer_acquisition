"""需求域对外 DTO。

跨域和对 API 暴露的形状。与 ``models.py`` 分开的原因：
实体字段会因存储需要而变化，DTO 是承诺给外部的形状，两者变化节奏不同。

**View 类必须携带 provenance 摘要**——前端要能展示"这个数字从哪来"，
这是硬边界 4 的落地出口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from shared.schemas.money import Money


@dataclass(frozen=True)
class HypothesisDiscoveryEvidenceView:
    """账户发现可消费的单条信号投影；保留内部 signal ID 与公开来源。"""

    signal_id: str
    summary: str
    source_url: str | None


@dataclass(frozen=True)
class HypothesisDiscoveryView:
    """账户发现模型安全投影；推断与支撑信号仍在结构上分离。"""

    hypothesis_id: str
    category: str
    reasoning: str
    evidence: tuple[HypothesisDiscoveryEvidenceView, ...]
    source_signal_refs: tuple[str, ...]


@dataclass(frozen=True)
class SignalCaptureRequest:
    """记录信号的入参。

    字段：
        signal_type, entity_name, raw_observation
        source_id:     来源身份（网页=页面哈希；非网页=调用方提供的记录 identity）
        extracted_by:  提取者（模型版本标识或 "human"）
        possible_need:  可能的需求方向（参考，不是结论）
        source_url:     网页来源必填
        page_hash:      网页来源必填
        observed_at
        source_type:    见 ``SourceType``

    校验：``source_type == WEB_PAGE`` 时 ``source_url`` 与 ``page_hash``
    必填，且 ``source_id == page_hash``。服务层拒绝不合格入参，不做"友好补全"。
    """

    signal_type: str
    entity_name: str
    raw_observation: str
    observed_at: datetime
    source_type: str
    source_id: str
    """来源身份：网页类 = 页面哈希；非网页类 = message/upload/provider/
    员工录入记录 identity（调用方提供）。"""
    extracted_by: str
    """提取者（模型版本标识或 "human"；Provenance 要求具体版本）。"""
    possible_need: str | None = None
    source_url: str | None = None
    page_hash: str | None = None


@dataclass(frozen=True)
class EvidenceSummary:
    """证据摘要，供界面展示"为什么这么判断"。

    字段：
        level:        证据等级
        summary:      一句话说明
        source_url:   可点击的来源
        source_ref:   消息/上传的引用标识
        observed_at
    """

    level: str
    summary: str
    observed_at: datetime
    source_url: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class HypothesisView:
    """需求假设视图。

    界面必须把 ``reasoning`` 明确标注为**推断**，并可展开
    ``evidence`` 看依据。不能显示成和已验证需求一样的样式——
    那会让老板误以为这是事实。

    字段：
        hypothesis_id, account_id, account_name, category
        reasoning:        推断理由
        confidence_tier:  档位（离散值，不是小数）
        confidence_explanation: 为什么是这一档，人类可读
        evidence:         证据列表
        status, created_at
        is_inference:     恒为 True，提醒前端渲染成推断样式
    """

    hypothesis_id: str
    account_id: str
    account_name: str
    category: str
    reasoning: str
    confidence_tier: str
    confidence_explanation: str
    evidence: list[EvidenceSummary]
    status: str
    created_at: datetime
    is_inference: bool = True


@dataclass(frozen=True)
class NeedFieldView:
    """需求的单个字段 + 来源。

    字段：
        name:          字段名
        value:         显示值
        source_ref:    来源引用（消息 ID / 上传 ID）
        source_quote:  客户原话摘录——**最有说服力的展示**，
                       员工看到原话就知道该怎么接
        confirmed_by:  确认人，None 表示仅模型提取未经人工确认
    """

    name: str
    value: str
    source_ref: str
    source_quote: str | None = None
    confirmed_by: str | None = None


@dataclass(frozen=True)
class ValidatedNeedView:
    """已验证需求视图。

    字段：
        need_id, account_id, account_name
        product_category
        fields:              全部字段 + 来源
        completeness:        0–5
        missing_for_sourcing: 还缺什么才能寻源
        status, created_at
        quantity, destination, required_by, target_price
            —— 常用字段的便捷访问，值本身在 ``fields`` 里有完整来源
    """

    need_id: str
    account_id: str
    account_name: str
    product_category: str
    fields: list[NeedFieldView]
    completeness: int
    missing_for_sourcing: list[str]
    status: str
    created_at: datetime
    quantity: int | None = None
    destination: str | None = None
    required_by: date | None = None
    target_price: Money | None = None


@dataclass(frozen=True)
class NeedClusterView:
    """需求簇视图。

    字段：
        cluster_id, category
        member_count, countries
        total_potential_quantity
        recurring_demand
        member_needs:  成员需求摘要
        suggests_catalog_product: 是否达到提议入正式目录的规模
    """

    cluster_id: str
    category: str
    member_count: int
    countries: list[str]
    member_needs: list[ValidatedNeedView]
    total_potential_quantity: int | None = None
    recurring_demand: bool | None = None
    suggests_catalog_product: bool = False
