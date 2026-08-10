"""发件身份纯领域模型（内部实现）。

本模块只依赖 ``shared``：不持有凭证、不访问 DNS/Gmail，也不把比率交给
浮点运算。外部域只能经 ``schemas.py`` 与 ``service.py`` 消费本域能力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from domains.sending_identity.errors import (
    InvalidConnectorReferenceError,
    InvalidSendingAddressError,
    InvalidSendingDomainError,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import SendingIdentityId, TenantId


class DomainRole(str, Enum):
    """域名角色；只有 ``COLD_OUTREACH`` 可承载冷开发。"""

    COLD_OUTREACH = "cold_outreach"
    PRIMARY_BUSINESS = "primary_business"
    TRANSACTIONAL = "transactional"


class IdentityState(str, Enum):
    """发件身份显式状态机。``RETIRED`` 没有后继。"""

    CREATED = "created"
    AUTH_PENDING = "auth_pending"
    WARMING = "warming"
    ACTIVE = "active"
    THROTTLED = "throttled"
    SUSPENDED = "suspended"
    RETIRED = "retired"


ALLOWED_TRANSITIONS: dict[IdentityState, frozenset[IdentityState]] = {
    IdentityState.CREATED: frozenset({IdentityState.AUTH_PENDING, IdentityState.RETIRED}),
    IdentityState.AUTH_PENDING: frozenset({IdentityState.WARMING, IdentityState.RETIRED}),
    IdentityState.WARMING: frozenset(
        {IdentityState.ACTIVE, IdentityState.THROTTLED, IdentityState.SUSPENDED, IdentityState.RETIRED}
    ),
    IdentityState.ACTIVE: frozenset(
        {IdentityState.THROTTLED, IdentityState.SUSPENDED, IdentityState.RETIRED}
    ),
    IdentityState.THROTTLED: frozenset(
        {IdentityState.WARMING, IdentityState.ACTIVE, IdentityState.SUSPENDED, IdentityState.RETIRED}
    ),
    IdentityState.SUSPENDED: frozenset(
        {IdentityState.WARMING, IdentityState.ACTIVE, IdentityState.RETIRED}
    ),
    IdentityState.RETIRED: frozenset(),
}


class AuthCheck(str, Enum):
    SPF = "spf"
    DKIM = "dkim"
    DMARC = "dmarc"


class AuthenticationFailureCategory(str, Enum):
    RECORD_MISSING = "record_missing"
    RECORD_INVALID = "record_invalid"
    ALIGNMENT_FAILED = "alignment_failed"
    POLICY_INSUFFICIENT = "policy_insufficient"
    LOOKUP_UNAVAILABLE = "lookup_unavailable"


class AuthenticationFixInstruction(str, Enum):
    """固定修复代码，而非会泄漏 DNS 原文的自由文本。"""

    CONFIGURE_SPF = "configure_spf"
    CONFIGURE_DKIM = "configure_dkim"
    CONFIGURE_DMARC = "configure_dmarc"
    FIX_ALIGNMENT = "fix_alignment"
    STRENGTHEN_POLICY = "strengthen_policy"
    RETRY_LOOKUP = "retry_lookup"


class DeliveryEventType(str, Enum):
    DELIVERED = "delivered"
    HARD_BOUNCED = "hard_bounced"
    SOFT_BOUNCED = "soft_bounced"
    COMPLAINT = "complaint"
    UNSUBSCRIBED = "unsubscribed"
    SPAM_TRAP = "spam_trap"
    BLOCKLISTED = "blocklisted"


_REFERENCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")


def _is_utc_aware(value: datetime) -> bool:
    offset = value.utcoffset()
    return value.tzinfo is not None and offset is not None and offset.total_seconds() == 0


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_sending_domain(raw: str) -> str:
    """将合法 FQDN 统一为 lowercase、无根点的 IDNA ASCII 形式。"""
    if not isinstance(raw, str):
        raise InvalidSendingDomainError("发件域名格式无效")
    candidate = raw.strip().removesuffix(".")
    if not candidate or any(char.isspace() for char in candidate):
        raise InvalidSendingDomainError("发件域名格式无效")
    if any(token in candidate for token in (":", "/", "@")) or "." not in candidate:
        raise InvalidSendingDomainError("发件域名格式无效")
    try:
        normalized = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidSendingDomainError("发件域名格式无效") from exc
    if len(normalized) > 253:
        raise InvalidSendingDomainError("发件域名格式无效")
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise InvalidSendingDomainError("发件域名格式无效")
    return normalized


def validate_connector_ref(raw: str) -> str:
    """校验短引用名，不把 token、URL 或自由文本带入领域层。"""
    if not isinstance(raw, str) or not _REFERENCE_RE.fullmatch(raw):
        raise InvalidConnectorReferenceError("连接器引用格式无效")
    lowered = raw.lower()
    if any(word in lowered for word in ("bearer", "token", "secret", "password")):
        raise InvalidConnectorReferenceError("连接器引用格式无效")
    return raw


def normalize_sending_address(raw: str, normalized_domain: str) -> str:
    """规范化 Phase 1 ASCII 发件地址，并强制与登记域名一致。"""
    if not isinstance(raw, str):
        raise InvalidSendingAddressError("发件地址格式无效")
    domain = normalize_sending_domain(normalized_domain)
    candidate = raw.strip()
    if candidate.count("@") != 1:
        raise InvalidSendingAddressError("发件地址格式无效")
    local, raw_domain = candidate.split("@")
    if (
        not local
        or not local.isascii()
        or not _LOCAL_RE.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        raise InvalidSendingAddressError("发件地址格式无效")
    if normalize_sending_domain(raw_domain) != domain:
        raise InvalidSendingAddressError("发件地址格式无效")
    return f"{local.lower()}@{domain}"


def validate_identity_transition(current: IdentityState, target: IdentityState) -> None:
    """验证状态机的一条边；错误仅含安全的状态代码。"""
    if not isinstance(current, IdentityState) or not isinstance(target, IdentityState):
        raise InvalidStateTransition("发件身份状态转换无效")
    if target not in ALLOWED_TRANSITIONS[current]:
        allowed = ",".join(sorted(state.value for state in ALLOWED_TRANSITIONS[current]))
        raise InvalidStateTransition(f"发件身份状态不能从 {current.value} 转为 {target.value}；允许：{allowed}")


def recovery_state(restricted: IdentityState, saved: IdentityState) -> IdentityState:
    """从限制态恢复时只允许持久化的 ``WARMING`` 或 ``ACTIVE``。"""
    if restricted not in {IdentityState.THROTTLED, IdentityState.SUSPENDED} or saved not in {
        IdentityState.WARMING,
        IdentityState.ACTIVE,
    }:
        raise InvalidStateTransition("发件身份恢复状态无效")
    validate_identity_transition(restricted, saved)
    return saved


@dataclass(frozen=True)
class WarmupPlan:
    """固定 28 天预热曲线；调用方不能提供任意 schedule。"""

    started_on: date
    target_daily_volume: int

    def __post_init__(self) -> None:
        if not isinstance(self.started_on, date) or isinstance(self.started_on, datetime):
            raise ValidationError("预热开始日期无效")
        if not _is_real_int(self.target_daily_volume) or not 5 <= self.target_daily_volume <= 100:
            raise ValidationError("预热目标日发送量必须为 5 到 100 的整数")

    @classmethod
    def create(cls, started_on: date, target_daily_volume: int) -> WarmupPlan:
        return cls(started_on=started_on, target_daily_volume=target_daily_volume)

    def daily_limit_on(self, on_day: date) -> int:
        """返回指定日期的确定性额度；开始日前为零，第 29 天起为目标。"""
        if not isinstance(on_day, date) or isinstance(on_day, datetime):
            raise ValidationError("预热日期无效")
        day_number = (on_day - self.started_on).days + 1
        if day_number < 1:
            return 0
        target = self.target_daily_volume
        if day_number <= 3:
            return min(5, target)
        if day_number <= 7:
            return min(15, target)
        if day_number <= 14:
            return min(30, target)
        if day_number <= 21:
            return min(50, target)
        if day_number <= 28:
            base = min(50, target)
            numerator = (target - base) * (day_number - 21)
            return base + (numerator + 6) // 7
        return target

    def is_complete_on(self, on_day: date) -> bool:
        """第 29 个自然日才算完成，低 target 也不得缩短预热。"""
        return (on_day - self.started_on).days >= 28


@dataclass(frozen=True)
class ReputationWindow:
    """按滚动窗口计数派生信誉比率，所有比率均为 ``Decimal``。"""

    window_days: int
    computed_at: datetime
    sent_attempts: int
    delivered: int
    hard_bounced: int
    soft_bounced: int
    complaints: int
    unsubscribed: int
    spam_trap_hits: int = 0
    blocklist_hits: int = 0

    def __post_init__(self) -> None:
        if not _is_real_int(self.window_days) or self.window_days < 1 or not _is_utc_aware(self.computed_at):
            raise ValidationError("信誉窗口无效")
        if any(not _is_real_int(value) or value < 0 for value in self._counts()):
            raise ValidationError("信誉窗口计数无效")

    def _counts(self) -> tuple[int, ...]:
        return (
            self.sent_attempts,
            self.delivered,
            self.hard_bounced,
            self.soft_bounced,
            self.complaints,
            self.unsubscribed,
            self.spam_trap_hits,
            self.blocklist_hits,
        )

    @property
    def hard_bounce_rate(self) -> Decimal:
        return self._rate(self.hard_bounced)

    @property
    def complaint_rate(self) -> Decimal:
        return self._rate(self.complaints)

    @property
    def delivery_rate(self) -> Decimal:
        return self._rate(self.delivered)

    def _rate(self, numerator: int) -> Decimal:
        if self.sent_attempts == 0:
            return Decimal(0)
        return Decimal(numerator) / Decimal(self.sent_attempts)


def _valid_rate(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= Decimal(0)


@dataclass(frozen=True)
class ReputationThresholds:
    """熔断阈值，拒绝 float，防止临界比较引入二进制误差。"""

    throttle_hard_bounce_rate: Decimal = Decimal(".03")
    suspend_hard_bounce_rate: Decimal = Decimal(".05")
    throttle_complaint_rate: Decimal = Decimal(".001")
    suspend_complaint_rate: Decimal = Decimal(".003")
    suspend_on_spam_trap: bool = True
    suspend_on_blocklist: bool = True
    minimum_sample: int = 50

    def __post_init__(self) -> None:
        rates = (
            self.throttle_hard_bounce_rate,
            self.suspend_hard_bounce_rate,
            self.throttle_complaint_rate,
            self.suspend_complaint_rate,
        )
        if not all(_valid_rate(rate) for rate in rates):
            raise ValidationError("信誉阈值必须为非负 Decimal")
        if (
            self.throttle_hard_bounce_rate >= self.suspend_hard_bounce_rate
            or self.throttle_complaint_rate >= self.suspend_complaint_rate
        ):
            raise ValidationError("限流阈值必须严格低于停用阈值")
        if not isinstance(self.suspend_on_spam_trap, bool) or not isinstance(self.suspend_on_blocklist, bool):
            raise ValidationError("立即停用开关必须为 bool")
        if not _is_real_int(self.minimum_sample) or self.minimum_sample < 1:
            raise ValidationError("最小样本必须为正整数")


@dataclass
class SendingIdentity:
    """发件身份实体；只持有安全 connector 引用，不持有任何凭证。"""

    identity_id: SendingIdentityId
    tenant_id: TenantId
    address: str
    domain: str
    role: DomainRole
    created_at: datetime
    state: IdentityState = IdentityState.CREATED
    display_name: str | None = None
    warmup_plan: WarmupPlan | None = None
    thresholds: ReputationThresholds = field(default_factory=ReputationThresholds)
    activated_at: datetime | None = None
    suspended_at: datetime | None = None
    retired_at: datetime | None = None
    sendable_state_before_restriction: IdentityState | None = None
    connector_ref: str | None = None

    def __post_init__(self) -> None:
        self.domain = normalize_sending_domain(self.domain)
        self.address = normalize_sending_address(self.address, self.domain)
        if not isinstance(self.role, DomainRole) or not isinstance(self.state, IdentityState):
            raise ValidationError("发件身份枚举无效")
        if self.state in {IdentityState.THROTTLED, IdentityState.SUSPENDED}:
            if self.sendable_state_before_restriction not in {
                IdentityState.WARMING,
                IdentityState.ACTIVE,
            }:
                raise ValidationError("受限身份必须保留可发送前状态")
        elif self.sendable_state_before_restriction is not None:
            raise ValidationError("非受限身份不得保留可发送前状态")
        if not _is_utc_aware(self.created_at):
            raise ValidationError("发件身份创建时间必须为 UTC")
        if self.connector_ref is not None:
            self.connector_ref = validate_connector_ref(self.connector_ref)

    def can_send(self) -> bool:
        return self.state in {IdentityState.WARMING, IdentityState.ACTIVE}

    def may_be_used_for_cold_outreach(self) -> bool:
        return self.role is DomainRole.COLD_OUTREACH

    def transition_to(self, target: IdentityState) -> None:
        """执行唯一的状态变更，并守住受限状态的持久化恢复不变量。"""
        validate_identity_transition(self.state, target)
        restricted_states = {IdentityState.THROTTLED, IdentityState.SUSPENDED}
        sendable_states = {IdentityState.WARMING, IdentityState.ACTIVE}
        if target in restricted_states:
            if self.state in sendable_states:
                self.sendable_state_before_restriction = self.state
            elif (
                self.state in restricted_states
                and self.sendable_state_before_restriction not in sendable_states
            ):
                raise InvalidStateTransition("受限身份缺少可恢复状态")
        elif self.state in restricted_states and target in sendable_states:
            saved_state = self.sendable_state_before_restriction
            if not isinstance(saved_state, IdentityState) or saved_state not in sendable_states:
                raise InvalidStateTransition("受限身份缺少可恢复状态")
            if recovery_state(self.state, saved_state) is not target:
                raise InvalidStateTransition("受限身份只能恢复到持久化状态")
            self.sendable_state_before_restriction = None
        elif target not in restricted_states:
            self.sendable_state_before_restriction = None
        self.state = target
