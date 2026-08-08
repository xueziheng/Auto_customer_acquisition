"""发件身份域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from shared.schemas.identifiers import SendingIdentityId, TenantId


class DomainRole(str, Enum):
    """域名角色。**本域最重要的枚举。**"""

    COLD_OUTREACH = "cold_outreach"
    """冷开发专用域。视为消耗品——出问题就退役换新的。"""

    PRIMARY_BUSINESS = "primary_business"
    """主业务域。用于报价、合同、现有客户往来。

    **绝不允许用于冷发。** 这个域名被标记等于公司对外沟通能力停摆，
    且无法用「换一个」解决——客户认识的是这个域名。
    """

    TRANSACTIONAL = "transactional"
    """系统通知域。与冷开发分离，避免通知被冷发的信誉问题拖累。"""


class IdentityState(str, Enum):
    CREATED = "created"
    AUTH_PENDING = "auth_pending"
    """等待 SPF/DKIM/DMARC 校验通过。此状态不允许发送。"""

    WARMING = "warming"
    """预热中。发送量按预热天数受限。"""

    ACTIVE = "active"
    THROTTLED = "throttled"
    """指标超阈值自动限流。可自动恢复。"""

    SUSPENDED = "suspended"
    """严重超标自动停用。**需人工排查后才能恢复**——能自动恢复的停用
    起不到作用，问题会立刻复发。"""

    RETIRED = "retired"
    """已退役，不可逆。"""


ALLOWED_TRANSITIONS: dict[IdentityState, set[IdentityState]] = {
    IdentityState.CREATED: {IdentityState.AUTH_PENDING, IdentityState.RETIRED},
    IdentityState.AUTH_PENDING: {IdentityState.WARMING, IdentityState.RETIRED},
    IdentityState.WARMING: {
        IdentityState.ACTIVE,
        IdentityState.THROTTLED,
        IdentityState.SUSPENDED,
        IdentityState.RETIRED,
    },
    IdentityState.ACTIVE: {
        IdentityState.THROTTLED,
        IdentityState.SUSPENDED,
        IdentityState.RETIRED,
    },
    IdentityState.THROTTLED: {
        IdentityState.ACTIVE,
        IdentityState.SUSPENDED,
        IdentityState.RETIRED,
    },
    IdentityState.SUSPENDED: {IdentityState.ACTIVE, IdentityState.RETIRED},
    IdentityState.RETIRED: set(),
}
"""注意 ``AUTH_PENDING`` 不能直接到 ``ACTIVE``：必须走完预热。

跳过预热是新域名最常见的死法——邮件服务商看到从未发信的域名突然
日发上百封，直接判为垃圾源。
"""


class AuthCheck(str, Enum):
    SPF = "spf"
    DKIM = "dkim"
    DMARC = "dmarc"


@dataclass(frozen=True)
class AuthStatus:
    """认证校验结果。

    字段：
        checked_at
        results:  每项检查的通过情况
        details:  未通过项的具体原因（DNS 记录内容、报错）

    ``details`` 要具体到能照着修：写「SPF 记录缺少 include:...」，
    不要写「SPF 配置错误」。
    """

    checked_at: datetime
    results: dict[AuthCheck, bool]
    details: dict[str, str] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        """三项全过才算通过。**不接受部分通过。**"""
        raise NotImplementedError


@dataclass(frozen=True)
class WarmupPlan:
    """预热计划。

    字段：
        started_on
        target_daily_volume:  预热完成后的目标日发送量
        schedule:             第 N 天 → 当日上限
        completed_on

    ``schedule`` 建议形状（按天累进）：
        第 1–3 天    5/天
        第 4–7 天    10/天
        第 2 周      20/天
        第 3 周      40/天
        第 4 周      逐步到 target

    具体曲线可调，但两条不能破：起点必须低（≤10），爬升必须连续
    （不能今天 10 明天 100）。
    """

    started_on: date
    target_daily_volume: int
    schedule: dict[int, int]
    completed_on: date | None = None

    def daily_limit_on(self, day: date) -> int:
        """当日发送上限。

        实现要求：
        - 预热未完成时按 ``schedule`` 取对应天数的上限
        - 超出 schedule 覆盖范围则返回 ``target_daily_volume``
        - **这个方法是发送量的唯一权威来源**，不要在别处硬编码上限
        """
        raise NotImplementedError

    def is_complete_on(self, day: date) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class ReputationWindow:
    """滚动窗口信誉指标。

    **按窗口算，不按生命周期。** 生命周期平均值会掩盖最近的恶化：
    历史发 10000 封退信 1%，最近 200 封退信 15%，生命周期数字看着
    健康，实际已经在着火。

    字段：
        window_days
        computed_at
        sent, delivered
        hard_bounced, soft_bounced
        complaints, unsubscribes
        spam_trap_hits:     垃圾陷阱命中 —— **比退信更早的预警**
        blocklist_hits:     黑名单出现

    比率用属性算，不存——存下来会和计数不一致。
    """

    window_days: int
    computed_at: datetime
    sent: int
    delivered: int
    hard_bounced: int
    soft_bounced: int
    complaints: int
    unsubscribes: int
    spam_trap_hits: int = 0
    blocklist_hits: int = 0

    @property
    def hard_bounce_rate(self) -> float:
        """硬退信率。``sent`` 为 0 时返回 0。

        硬退信是最重要的指标：它直接说明联系人数据质量差，
        而邮件服务商对此惩罚最重。
        """
        raise NotImplementedError

    @property
    def complaint_rate(self) -> float:
        """投诉率。阈值比退信率低一个量级——投诉的杀伤力更大。"""
        raise NotImplementedError

    @property
    def delivery_rate(self) -> float:
        raise NotImplementedError


@dataclass(frozen=True)
class ReputationThresholds:
    """熔断阈值。

    默认值参考行业通行标准，但**必须可按租户调整**——不同市场和
    数据源质量差异很大。

    字段：
        throttle_hard_bounce_rate:  默认 0.03
        suspend_hard_bounce_rate:   默认 0.05
        throttle_complaint_rate:    默认 0.001
        suspend_complaint_rate:     默认 0.003
        suspend_on_spam_trap:       默认 True，命中即停
        suspend_on_blocklist:       默认 True
        minimum_sample:             默认 50，样本不足不判定

    ``minimum_sample`` 很重要：发了 10 封退 1 封就是 10% 退信率，
    但那不说明任何问题。没有这个下限，新身份会在预热第一天被自己
    的熔断机制停掉。
    """

    throttle_hard_bounce_rate: float = 0.03
    suspend_hard_bounce_rate: float = 0.05
    throttle_complaint_rate: float = 0.001
    suspend_complaint_rate: float = 0.003
    suspend_on_spam_trap: bool = True
    suspend_on_blocklist: bool = True
    minimum_sample: int = 50


@dataclass
class SendingIdentity:
    """发件身份。

    字段：
        identity_id, tenant_id
        address:        发件地址
        domain:         所属域名
        role:           域名角色
        display_name
        state
        auth_status
        warmup_plan
        thresholds
        created_at, activated_at, suspended_at
        suspension_reason
        connector_ref:  connectors/ 里对应的凭据引用（**不是凭据本身**，
                        密钥永远不进领域层，见硬边界 2）
    """

    identity_id: SendingIdentityId
    tenant_id: TenantId
    address: str
    domain: str
    role: DomainRole
    created_at: datetime
    state: IdentityState = IdentityState.CREATED
    display_name: str | None = None
    auth_status: AuthStatus | None = None
    warmup_plan: WarmupPlan | None = None
    thresholds: ReputationThresholds = field(default_factory=ReputationThresholds)
    activated_at: datetime | None = None
    suspended_at: datetime | None = None
    suspension_reason: str | None = None
    connector_ref: str | None = None

    def can_send(self) -> bool:
        """现在能不能发。

        实现要求：仅 ``ACTIVE`` 与 ``WARMING`` 返回 True。
        ``THROTTLED`` 返回 False——限流的含义是「暂时完全停止新发送」，
        不是「少发一点」。少发一点的语义靠 ``daily_limit_on`` 表达，
        混在一起会让判断逻辑到处分叉。
        """
        raise NotImplementedError

    def may_be_used_for_cold_outreach(self) -> bool:
        """能否用于冷开发。

        实现要求：``role`` 必须是 ``COLD_OUTREACH``。
        其他角色返回 False，**没有例外参数**。
        """
        raise NotImplementedError


@dataclass(frozen=True)
class DomainReputation:
    """域名级聚合信誉。

    存在的理由：一个坏身份会拖累整个域名。三个身份各自都在阈值边缘
    时，单看每一个都不超标，但域名整体已经有风险。

    字段：
        domain, role
        identity_count, active_count
        window:                 聚合后的滚动窗口指标
        at_risk:                是否处于风险状态
        worst_identity:         指标最差的身份
    """

    domain: str
    role: DomainRole
    identity_count: int
    active_count: int
    window: ReputationWindow
    at_risk: bool
    worst_identity: SendingIdentityId | None = None
