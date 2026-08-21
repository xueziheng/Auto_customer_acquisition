"""需求域实体。

四层：``DemandSignal`` → ``NeedHypothesis`` → ``ValidatedNeed`` → ``NeedCluster``

**内部实现，其他域不得导入。** 跨域用 ``schemas.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from shared.errors import ValidationError
from shared.schemas.evidence import EvidenceItem, EvidenceLevel
from shared.schemas.identifiers import (
    ConversationId,
    DemandSignalId,
    EmployeeId,
    MessageId,
    NeedClusterId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import Money
from shared.schemas.provenance import FactualField, InferredField, Provenance

# 硬边界 5/6 证据等级映射表（写成常量不做 if 链；finding 4 仅三直接档）
_CUSTOMER_DIRECT_SIGNAL_TYPES = frozenset(
    {"public_rfq", "inbound_inquiry", "tender_notice"}
)
_COMPANY_EVENT_SIGNAL_TYPES = frozenset(
    {
        "product_line_expansion",
        "facility_expansion",
        "new_market_entry",
        "procurement_role_hiring",
        "distributor_change",
        "new_certification",
        "large_contract_won",
        "funding_or_merger",
    }
)
_EVIDENCE_LEVEL_RANK: dict[EvidenceLevel, int] = {
    level: i for i, level in enumerate(EvidenceLevel)
}
_PROMOTABLE_SOURCE_TYPES = frozenset({"conversation", "upload", "employee_input"})
_AGENT_INFERENCE_SIGNAL_TYPES = frozenset(
    {
        "trade_show_request",
        "historical_unclosed_need",
        "supplier_referral",
        "stockout_observed",
        "negative_product_review",
        "supplier_complaint",
        "marketplace_seller_activity",
        "catalog_gap",
        "value_chain_adjacency",
        "complementary_category",
    }
)


class SignalType(str, Enum):
    """需求信号类型。

    这个枚举直接决定 Agent 去搜什么。设计稿第六节列出的发现路径
    在这里落地为可穷举的类型——含糊的"市场情报"无法编码成搜索策略。
    """

    # --- 直接需求（最有价值：客户自己说的） ---
    PUBLIC_RFQ = "public_rfq"
    TENDER_NOTICE = "tender_notice"
    INBOUND_INQUIRY = "inbound_inquiry"
    TRADE_SHOW_REQUEST = "trade_show_request"
    HISTORICAL_UNCLOSED_NEED = "historical_unclosed_need"
    SUPPLIER_REFERRAL = "supplier_referral"

    # --- 企业变化信号（有观察，需求仍是推断） ---
    PRODUCT_LINE_EXPANSION = "product_line_expansion"
    FACILITY_EXPANSION = "facility_expansion"
    NEW_MARKET_ENTRY = "new_market_entry"
    PROCUREMENT_ROLE_HIRING = "procurement_role_hiring"
    DISTRIBUTOR_CHANGE = "distributor_change"
    NEW_CERTIFICATION = "new_certification"
    LARGE_CONTRACT_WON = "large_contract_won"
    FUNDING_OR_MERGER = "funding_or_merger"

    # --- 供应问题信号（对现有供应不满） ---
    STOCKOUT_OBSERVED = "stockout_observed"
    NEGATIVE_PRODUCT_REVIEW = "negative_product_review"
    SUPPLIER_COMPLAINT = "supplier_complaint"

    # --- 卖家/店铺反向发现 ---
    MARKETPLACE_SELLER_ACTIVITY = "marketplace_seller_activity"
    CATALOG_GAP = "catalog_gap"

    # --- 产业链推断 ---
    VALUE_CHAIN_ADJACENCY = "value_chain_adjacency"
    COMPLEMENTARY_CATEGORY = "complementary_category"


class SignalStatus(str, Enum):
    CAPTURED = "captured"
    LINKED_TO_HYPOTHESIS = "linked_to_hypothesis"
    DISCARDED = "discarded"


@dataclass
class DemandSignal:
    """需求信号：观察到的事实。

    **不可变。** 观察到新情况是新信号，不是更新旧信号——否则无法
    回答"我们当时看到的是什么"。

    字段：
        signal_id, tenant_id
        signal_type:    信号类型
        entity_name:    观察到的企业名（原始文本，未经实体消歧）
        raw_observation: 原始观察内容
        possible_need:  可能的需求方向（自由文本，仅供参考，不是结论）
        provenance:     来源（网页类必须有 URL 和 page_hash）
        observed_at:    观察时间
        status:         状态
        account_id:     消歧后关联到的企业，初始为 None
        discard_reason: 丢弃原因
    """

    signal_id: DemandSignalId
    tenant_id: TenantId
    signal_type: SignalType
    entity_name: str
    raw_observation: str
    provenance: Provenance
    observed_at: datetime
    status: SignalStatus = SignalStatus.CAPTURED
    possible_need: str | None = None
    account_id: ProspectAccountId | None = None
    discard_reason: str | None = None

    @property
    def evidence_level(self) -> EvidenceLevel:
        """信号对应的证据等级（映射表见模块级常量；spec D3/finding 4）。

        公开 RFQ/入站询盘/招标公告 → CUSTOMER_INTEREST_REPLY（规格内容不升级）；
        企业变化类 → PUBLIC_COMPANY_EVENT；产业链推断类 → AGENT_INDUSTRY_INFERENCE。
        """
        # 防御：类型注解保证为 SignalType，但直插/遗留对象可能携带裸字符串
        # （测试覆盖未知值 → ValidationError）；枚举成员取 .value
        value = (
            self.signal_type.value
            if isinstance(self.signal_type, SignalType)
            else str(self.signal_type)
        )
        if value in _CUSTOMER_DIRECT_SIGNAL_TYPES:
            return EvidenceLevel.CUSTOMER_INTEREST_REPLY
        if value in _COMPANY_EVENT_SIGNAL_TYPES:
            return EvidenceLevel.PUBLIC_COMPANY_EVENT
        if value in _AGENT_INFERENCE_SIGNAL_TYPES:
            return EvidenceLevel.AGENT_INDUSTRY_INFERENCE
        raise ValidationError("未知信号类型")


class HypothesisStatus(str, Enum):
    INFERRED = "inferred"
    CONTACTING = "contacting"
    VALIDATED = "validated"
    REJECTED = "rejected"


@dataclass
class NeedHypothesis:
    """需求假设：由信号推出的推断。

    **仍然不是事实。** 界面上必须显示为推断，并可展开看依据。

    字段：
        hypothesis_id, tenant_id
        account_id:     目标企业
        category:       推测的产品类别
        reasoning:      推断理由（``InferredField``，强制携带 based_on）
        signal_ids:     支撑它的信号
        status:         状态
        created_at
        rejection_reason: 被拒原因（``LossReason``），rejected 时必填
        validated_need_id: 晋升后指向的已验证需求

    注意**没有 confidence 字段**（硬边界 3）。置信度用
    ``derive_confidence(self.evidence())`` 现算。
    """

    hypothesis_id: NeedHypothesisId
    tenant_id: TenantId
    account_id: ProspectAccountId
    category: str
    reasoning: InferredField[str]
    signal_ids: list[DemandSignalId]
    created_at: datetime
    status: HypothesisStatus = HypothesisStatus.INFERRED
    rejection_reason: str | None = None
    validated_need_id: ValidatedNeedId | None = None

    def evidence(self) -> list[EvidenceItem]:
        """汇总证据（纯域内、零 IO，spec D1）：based_on 按 (source_type,
        source_id) 去重，组内保留等级最高一条，并列取先出现；输出保持组首现顺序。"""
        by_source: dict[tuple[str, str], EvidenceItem] = {}
        order: list[tuple[str, str]] = []
        for item in self.reasoning.based_on:
            key = (item.source_type, item.source_id)
            current = by_source.get(key)
            if current is None:
                by_source[key] = item
                order.append(key)
            elif _EVIDENCE_LEVEL_RANK[item.level] > _EVIDENCE_LEVEL_RANK[current.level]:
                by_source[key] = item
        return [by_source[key] for key in order]

    def can_promote_to_validated(self) -> bool:
        """唯一通过条件：存在证据等级 ≥ CUSTOMER_INTEREST_REPLY 且来源类型
        属于 conversation/upload/employee_input。Agent 推断不论多少条都不通过。"""
        required_rank = _EVIDENCE_LEVEL_RANK[EvidenceLevel.CUSTOMER_INTEREST_REPLY]
        return any(
            _EVIDENCE_LEVEL_RANK[item.level] >= required_rank
            and item.source_type in _PROMOTABLE_SOURCE_TYPES
            for item in self.evidence()
        )


class NeedStatus(str, Enum):
    VALIDATED = "validated"
    SOURCING_READY = "sourcing_ready"
    HANDED_TO_SOURCING = "handed_to_sourcing"
    FULFILLED = "fulfilled"
    WITHDRAWN = "withdrawn"
    LOST = "lost"


@dataclass
class ValidatedNeed:
    """已验证需求：客户本人确认过的采购需求。

    每个业务字段都是 ``FactualField``——必须能点到客户说这句话的
    那条消息。这是老板信任"高意向"判断的基础。

    字段（除标注外均可为 None，完整度据此推导）：
        need_id, tenant_id, account_id
        product_category:   产品类别（必填，1 级门槛）
        source_message_id:  确认需求的那条消息
        source_conversation_id
        status, created_at
        application:        用途
        material, size_spec, quantity, packaging
        destination:        目的地
        required_by:        交付时间
        target_price:       目标价（``Money``）
        current_supply_issue: 现有供应方案的问题——**最有价值的字段之一**，
                            它直接说明我们凭什么能赢
        certification_required
        confirmed_by:       确认的员工
        cluster_id:         归属的需求簇
    """

    need_id: ValidatedNeedId
    tenant_id: TenantId
    account_id: ProspectAccountId
    product_category: FactualField[str]
    source_message_id: MessageId
    created_at: datetime
    status: NeedStatus = NeedStatus.VALIDATED
    source_conversation_id: ConversationId | None = None
    application: FactualField[str] | None = None
    material: FactualField[str] | None = None
    size_spec: FactualField[str] | None = None
    quantity: FactualField[int] | None = None
    packaging: FactualField[str] | None = None
    destination: FactualField[str] | None = None
    required_by: FactualField[date] | None = None
    target_price: FactualField[Money] | None = None
    current_supply_issue: FactualField[str] | None = None
    certification_required: FactualField[str] | None = None
    confirmed_by: EmployeeId | None = None
    cluster_id: NeedClusterId | None = None

    @property
    def completeness(self) -> int:
        """需求完整度 0–5。**由字段推导，不可手动设置。**

        累积阶梯 = 最高连续满足级别（plan 2026-08-16-demand-completeness-derivation）：

        ```text
        0  product_category 无（防御性处理运行时非法/遗留对象；类型契约仍要求它）
        1  product_category 有，application 与 size_spec 都无
        2  application 或 size_spec 至少一个有，但 quantity 无
        3  quantity 有，但 destination 或 required_by 至少一个无  ← 寻源门槛
        4  destination 与 required_by 都有，但 material 或 size_spec 至少一个无
        5  累积前置全部满足（material 与 size_spec 都有；application 不因
           level 5 单独强制——size_spec 最终必有即满足 level 2 的 OR）
        ```

        presence 只判 ``FactualField is None``，不检查 value 真值、不做字段值
        业务验证（quantity=0 也算存在）。手动设置会被乐观填高，然后寻源门槛
        失效——那会导致带着模糊需求去问供应商，拿不到可用报价还消耗信誉。
        """
        if self.product_category is None:
            return 0
        if self.application is None and self.size_spec is None:
            return 1
        if self.quantity is None:
            return 2
        if self.destination is None or self.required_by is None:
            return 3
        if self.material is None or self.size_spec is None:
            return 4
        return 5

    def is_sourcing_ready(self) -> bool:
        """是否可以进寻源。要求完整度 ≥ 3（数量明确）。"""
        return self.completeness >= 3

    def missing_fields_for_sourcing(self) -> list[str]:
        """还缺什么才能寻源（只返回当前阻塞 level-3 门槛的最低层）。

        固定顺序分支（plan 2026-08-16-demand-completeness-derivation）：
        ``["product_category"]`` → ``["application", "size_spec"]``（替代条件
        成对）→ ``["quantity"]`` → ``[]``（completeness ≥ 3）。不列出
        destination/required_by/material 等后续完整化字段——函数名与本域权威
        门槛是进入 sourcing_ready（≥3），不把后续所有字段一次抛给客户。
        供 ``qualification_agent`` 决定下一个该问客户什么——一次只问最关键的
        一两项，不要抛出十几个问题。
        """
        if self.product_category is None:
            return ["product_category"]
        if self.application is None and self.size_spec is None:
            return ["application", "size_spec"]
        if self.quantity is None:
            return ["quantity"]
        return []


@dataclass
class NeedCluster:
    """需求簇：多个客户的相似需求。

    设计稿第 45.4 与 6.8 指出的机制：八个客户都要不锈钢船用铰链时，
    价值远大于八条独立需求——一次寻源服务多个买家、有谈价筹码、
    摊薄 MOQ 风险、还能沉淀成正式产品。

    **Phase 1 只记录，不驱动优先级。** 已验证需求只有个位数时聚不出
    东西。Phase 2 接寻源队列排序（见 ``ROADMAP.md``）。

    字段：
        cluster_id, tenant_id
        category:               统一后的类别
        member_need_ids
        countries:              涉及国家
        total_potential_quantity: 合计潜在数量
        recurring_demand:       是否可能重复采购
        created_at, updated_at
    """

    cluster_id: NeedClusterId
    tenant_id: TenantId
    category: str
    member_need_ids: list[ValidatedNeedId] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    total_potential_quantity: int | None = None
    recurring_demand: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def suggests_catalog_product(self) -> bool:
        """是否该提议进正式产品目录。

        Phase 2 用。判断依据：成员数、合计数量、重复采购迹象、
        跨国家分布。达标时通知产品负责人，不自动创建产品。
        """
        # Phase 2 才定义规模门槛；Phase 1 只能保守地不提议，不能自造阈值。
        return False
