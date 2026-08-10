"""发件身份域对外 DTO；不暴露凭证、原始 DNS 或 webhook payload。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from domains.sending_identity.errors import (
    InvalidAuthenticationResultError,
    InvalidDeliveryEventError,
)
from domains.sending_identity.models import (
    AuthCheck,
    AuthenticationFailureCategory,
    AuthenticationFixInstruction,
    DeliveryEventType,
    DomainRole,
    IdentityState,
    _is_real_int,
    _is_utc_aware,
    normalize_sending_address,
    normalize_sending_domain,
    validate_connector_ref,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, TenantId


def _is_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _require_utc_datetime(value: datetime, message: str) -> None:
    if not _is_utc_aware(value):
        raise TypeError(message)


@dataclass(frozen=True)
class IdentityRegisterRequest:
    """登记身份的安全输入；role 必须是本域 typed enum。"""

    address: str
    domain: str
    role: DomainRole
    display_name: str | None = None
    connector_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, DomainRole):
            raise TypeError("role 必须为 DomainRole")
        normalized_domain = normalize_sending_domain(self.domain)
        object.__setattr__(self, "domain", normalized_domain)
        object.__setattr__(self, "address", normalize_sending_address(self.address, normalized_domain))
        if self.connector_ref is not None:
            object.__setattr__(self, "connector_ref", validate_connector_ref(self.connector_ref))


@dataclass(frozen=True)
class AuthenticationFailure:
    """失败检查的固定类别和修复代码；不携带 DNS 记录或异常文本。"""

    check: AuthCheck
    category: AuthenticationFailureCategory
    instruction: AuthenticationFixInstruction

    def __post_init__(self) -> None:
        if not isinstance(self.check, AuthCheck) or not isinstance(self.category, AuthenticationFailureCategory) or not isinstance(self.instruction, AuthenticationFixInstruction):
            raise InvalidAuthenticationResultError("认证失败项无效")


@dataclass(frozen=True)
class AuthenticationResult:
    """一次 typed SPF/DKIM/DMARC 检查结果。"""

    checked_at: datetime
    spf_passed: bool
    dkim_passed: bool
    dmarc_passed: bool
    failures: tuple[AuthenticationFailure, ...]
    check_ref: str

    def __post_init__(self) -> None:
        if not _is_utc_aware(self.checked_at) or any(
            not isinstance(value, bool) for value in (self.spf_passed, self.dkim_passed, self.dmarc_passed)
        ):
            raise InvalidAuthenticationResultError("认证结果无效")
        try:
            validate_connector_ref(self.check_ref)
        except ValidationError as exc:
            raise InvalidAuthenticationResultError("认证结果无效") from exc
        if not isinstance(self.failures, tuple) or not all(isinstance(item, AuthenticationFailure) for item in self.failures):
            raise InvalidAuthenticationResultError("认证结果无效")
        failed_checks = {
            check
            for check, passed in (
                (AuthCheck.SPF, self.spf_passed),
                (AuthCheck.DKIM, self.dkim_passed),
                (AuthCheck.DMARC, self.dmarc_passed),
            )
            if not passed
        }
        failure_checks = tuple(failure.check for failure in self.failures)
        if set(failure_checks) != failed_checks or len(set(failure_checks)) != len(failure_checks):
            raise InvalidAuthenticationResultError("认证失败项与检查状态不一致")

    @property
    def all_passed(self) -> bool:
        return self.spf_passed and self.dkim_passed and self.dmarc_passed


@dataclass(frozen=True)
class DeliveryEventRecord:
    """可幂等记录的投递事件；不携带邮件正文或 provider payload。"""

    tenant_id: TenantId
    identity_id: SendingIdentityId
    event_type: DeliveryEventType
    occurred_at: datetime
    dedup_key: IdempotencyKey
    source_ref: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tenant_id, str)
            or not self.tenant_id
            or not isinstance(self.identity_id, str)
            or not self.identity_id
            or not isinstance(self.event_type, DeliveryEventType)
            or not isinstance(self.occurred_at, datetime)
            or not _is_utc_aware(self.occurred_at)
        ):
            raise InvalidDeliveryEventError("投递事件无效")
        if not isinstance(self.dedup_key, str):
            raise InvalidDeliveryEventError("投递事件无效")
        cleaned_key = self.dedup_key.strip()
        lowered_key = cleaned_key.lower()
        if (
            not 1 <= len(cleaned_key) <= 200
            or any(character.isspace() for character in cleaned_key)
            or any(
                unicodedata.category(character).startswith("C")
                for character in cleaned_key
            )
            or any(
                marker in lowered_key
                for marker in ("bearer", "token", "secret", "password")
            )
        ):
            raise InvalidDeliveryEventError("投递事件无效")
        object.__setattr__(self, "dedup_key", IdempotencyKey(cleaned_key))
        try:
            validate_connector_ref(self.source_ref)
        except ValidationError as exc:
            raise InvalidDeliveryEventError("投递事件无效") from exc


@dataclass(frozen=True)
class SendReservation:
    """已原子占用的发送名额；reservation 不退款。"""

    reservation_id: str
    identity_id: SendingIdentityId
    reservation_key: IdempotencyKey
    on_day: date
    sequence: int
    daily_limit: int
    remaining_today: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reservation_id, str)
            or not self.reservation_id.strip()
            or not isinstance(self.on_day, date)
            or isinstance(self.on_day, datetime)
            or not all(_is_real_int(value) for value in (self.sequence, self.daily_limit, self.remaining_today))
            or self.sequence < 1
            or self.daily_limit < 0
            or not 0 <= self.remaining_today <= self.daily_limit
        ):
            raise ValidationError("发送预留无效")


@dataclass(frozen=True)
class SendPermission:
    """只读发送许可诊断；真实发送必须另走原子 reservation。"""

    allowed: bool
    remaining_today: int
    daily_limit: int
    identity_state: IdentityState
    reason: str | None = None
    is_warming: bool = False
    warmup_day: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool) or not isinstance(self.is_warming, bool) or not isinstance(self.identity_state, IdentityState):
            raise TypeError("发送许可类型无效")


@dataclass(frozen=True)
class NearestThresholdView:
    """最接近阈值的确定性值与距离。"""

    name: str
    value: Decimal
    distance: Decimal

    def __post_init__(self) -> None:
        if not _is_decimal(self.value) or not _is_decimal(self.distance):
            raise TypeError("阈值视图必须使用 Decimal")


@dataclass(frozen=True)
class ReputationView:
    """信誉视图，全部比率/阈值使用 Decimal。"""

    window_days: int
    sent_attempts: int
    delivered: int
    hard_bounce_rate: Decimal
    complaint_rate: Decimal
    delivery_rate: Decimal
    computed_at: datetime
    spam_trap_hits: int = 0
    blocklist_hits: int = 0
    sample_sufficient: bool = True
    nearest_threshold: NearestThresholdView | None = None

    def __post_init__(self) -> None:
        if not all(_is_decimal(value) for value in (self.hard_bounce_rate, self.complaint_rate, self.delivery_rate)):
            raise TypeError("信誉比率必须使用 Decimal")
        _require_utc_datetime(self.computed_at, "信誉计算时间必须为 UTC")
        if not isinstance(self.sample_sufficient, bool):
            raise TypeError("样本充足标识必须为 bool")


@dataclass(frozen=True)
class DomainReputationView:
    """规范化域名的聚合信誉视图。"""

    domain: str
    role: DomainRole
    identity_count: int
    active_count: int
    reputation: ReputationView
    at_risk: bool
    worst_identity_id: SendingIdentityId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, DomainRole) or not isinstance(self.at_risk, bool):
            raise TypeError("域名信誉视图类型无效")
        object.__setattr__(self, "domain", normalize_sending_domain(self.domain))


@dataclass(frozen=True)
class AuthStatusView:
    """安全认证视图：仅暴露固定 failure 代码。"""

    checked_at: datetime
    spf_passed: bool
    dkim_passed: bool
    dmarc_passed: bool
    failures: tuple[AuthenticationFailure, ...] = ()

    def __post_init__(self) -> None:
        _require_utc_datetime(self.checked_at, "认证检查时间必须为 UTC")
        if any(not isinstance(value, bool) for value in (self.spf_passed, self.dkim_passed, self.dmarc_passed)):
            raise TypeError("认证状态必须为 bool")

    @property
    def all_passed(self) -> bool:
        return self.spf_passed and self.dkim_passed and self.dmarc_passed


@dataclass(frozen=True)
class IdentityView:
    """不含 connector/check 引用或调查文本的身份视图。"""

    identity_id: SendingIdentityId
    address: str
    domain: str
    role: DomainRole
    state: IdentityState
    created_at: datetime
    auth: AuthStatusView | None = None
    reputation: ReputationView | None = None
    warmup_day: int | None = None
    warmup_complete: bool = False
    target_daily_volume: int | None = None
    can_send_today: bool = False
    remaining_today: int = 0
    usable_for_cold_outreach: bool = False
    activated_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_utc_datetime(self.created_at, "身份创建时间必须为 UTC")
        if self.activated_at is not None:
            _require_utc_datetime(self.activated_at, "身份激活时间必须为 UTC")
        if not isinstance(self.role, DomainRole) or not isinstance(self.state, IdentityState):
            raise TypeError("身份视图枚举无效")
        if any(not isinstance(value, bool) for value in (self.warmup_complete, self.can_send_today, self.usable_for_cold_outreach)):
            raise TypeError("身份视图标识必须为 bool")


@dataclass(frozen=True)
class WarmupProgressView:
    """固定曲线的安全展示 DTO。"""

    started_on: date
    day_number: int
    today_limit: int
    target_daily_volume: int
    schedule: tuple[int, ...]
    estimated_complete_on: date | None = None
    is_complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.is_complete, bool) or not isinstance(self.schedule, tuple):
            raise TypeError("预热进度类型无效")
