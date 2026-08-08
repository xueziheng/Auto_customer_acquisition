"""发件身份域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class IdentityRegisterRequest:
    """登记发件身份的入参。

    字段：
        address, domain
        role:           域名角色（字符串，见 ``DomainRole``）
        display_name
        connector_ref:  凭据引用，**不是凭据本身**（硬边界 2）
    """

    address: str
    domain: str
    role: str
    display_name: str | None = None
    connector_ref: str | None = None


@dataclass(frozen=True)
class SendPermission:
    """发送许可 —— ``tool_gateway`` 发送前查的结果。

    字段：
        allowed
        remaining_today:   今日剩余额度
        daily_limit:       今日上限（预热期是当天的爬升值）
        reason:            不允许时的原因，人类可读
        identity_state
        is_warming
        warmup_day:        预热第几天

    ``reason`` 会展示给老板和员工，要说清「为什么今天只能发 10 封」，
    否则会被当成系统故障来报。
    """

    allowed: bool
    remaining_today: int
    daily_limit: int
    identity_state: str
    reason: str | None = None
    is_warming: bool = False
    warmup_day: int | None = None


@dataclass(frozen=True)
class ReputationView:
    """信誉视图。

    字段：
        window_days
        sent, delivered
        hard_bounce_rate, complaint_rate, delivery_rate
        spam_trap_hits, blocklist_hits
        computed_at
        sample_sufficient:  样本是否足够判定
        nearest_threshold:  最接近的阈值和距离，供预警展示
    """

    window_days: int
    sent: int
    delivered: int
    hard_bounce_rate: float
    complaint_rate: float
    delivery_rate: float
    computed_at: datetime
    spam_trap_hits: int = 0
    blocklist_hits: int = 0
    sample_sufficient: bool = True
    nearest_threshold: str | None = None


@dataclass(frozen=True)
class AuthStatusView:
    """认证状态视图。

    字段：
        checked_at
        spf_passed, dkim_passed, dmarc_passed
        all_passed
        fix_instructions:  未通过项的修复说明，要具体到能照着改
    """

    checked_at: datetime
    spf_passed: bool
    dkim_passed: bool
    dmarc_passed: bool
    all_passed: bool
    fix_instructions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityView:
    """发件身份视图。

    字段：
        identity_id, address, domain
        role, role_label
        state
        auth:                认证状态
        reputation:          滚动窗口信誉
        warmup_day, warmup_complete, target_daily_volume
        can_send_today, remaining_today
        usable_for_cold_outreach
        suspension_reason
        created_at, activated_at

    ``usable_for_cold_outreach`` 单独给一个布尔字段，是为了让界面
    能直接过滤——不要让前端自己按 role 判断，那种判断迟早会写错一处。
    """

    identity_id: str
    address: str
    domain: str
    role: str
    role_label: str
    state: str
    created_at: datetime
    auth: AuthStatusView | None = None
    reputation: ReputationView | None = None
    warmup_day: int | None = None
    warmup_complete: bool = False
    target_daily_volume: int | None = None
    can_send_today: bool = False
    remaining_today: int = 0
    usable_for_cold_outreach: bool = False
    suspension_reason: str | None = None
    activated_at: datetime | None = None


@dataclass(frozen=True)
class WarmupProgressView:
    """预热进度视图。

    字段：
        started_on, day_number
        today_limit, target_daily_volume
        schedule:        完整曲线，供界面画进度
        estimated_complete_on
        is_complete
    """

    started_on: date
    day_number: int
    today_limit: int
    target_daily_volume: int
    schedule: dict[int, int]
    estimated_complete_on: date | None = None
    is_complete: bool = False
