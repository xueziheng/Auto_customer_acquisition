"""触达域 tenant-aware 存储 Protocol 与 typed 原子结果。

**内部实现，其他域不得导入。** 所有写入结果显式区分创建、幂等命中、
业务冲突和额度耗尽，service 不解析数据库异常字符串。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Protocol, Self, runtime_checkable

from domains.outreach.models import (
    ActionRecord,
    Campaign,
    CampaignVersion,
    DailyQuotaUsage,
    Enrollment,
    MessageAttempt,
    SuppressionEntry,
)
from domains.outreach.permissions import OutreachScope
from domains.outreach.schemas import SuppressionTarget
from shared.errors import ValidationError
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    CampaignId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    ProspectAccountId,
    TenantId,
)


class EnrollmentInsertStatus(str, Enum):
    CREATED = "created"
    EXISTING = "existing"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ACCOUNT_CONFLICT = "account_conflict"


class AppendStatus(str, Enum):
    CREATED = "created"
    EXISTING = "existing"
    CONFLICT = "conflict"


class DeliveryCorrelationBindStatus(str, Enum):
    BOUND = "bound"
    EXISTING = "existing"
    CONFLICT = "conflict"


class QuotaReservationStatus(str, Enum):
    RESERVED = "reserved"
    CAP_REACHED = "cap_reached"


def _validate_winner(
    status: Enum,
    winner: object | None,
    winner_type: type[object],
) -> None:
    has_winner = status.value in {"created", "existing", "reserved"}
    if has_winner != (winner is not None):
        raise ValidationError("原子写入结果与 winner 不匹配")
    if winner is not None and not isinstance(winner, winner_type):
        raise ValidationError("原子写入 winner 类型无效")


@dataclass(frozen=True)
class EnrollmentInsertResult:
    status: EnrollmentInsertStatus
    winner: Enrollment | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EnrollmentInsertStatus):
            raise ValidationError("Enrollment insert status 无效")
        _validate_winner(self.status, self.winner, Enrollment)


@dataclass(frozen=True)
class SuppressionAppendResult:
    status: AppendStatus
    winner: SuppressionEntry | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AppendStatus):
            raise ValidationError("Suppression append status 无效")
        _validate_winner(self.status, self.winner, SuppressionEntry)


@dataclass(frozen=True)
class QuotaReservationResult:
    status: QuotaReservationStatus
    winner: DailyQuotaUsage | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, QuotaReservationStatus):
            raise ValidationError("Quota reservation status 无效")
        _validate_winner(self.status, self.winner, DailyQuotaUsage)


@dataclass(frozen=True)
class MessageAttemptCreateResult:
    status: AppendStatus
    winner: MessageAttempt | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AppendStatus):
            raise ValidationError("Message Attempt create status 无效")
        _validate_winner(self.status, self.winner, MessageAttempt)


@dataclass(frozen=True)
class DeliveryCorrelationBindResult:
    status: DeliveryCorrelationBindStatus
    winner: MessageAttempt | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DeliveryCorrelationBindStatus):
            raise ValidationError("Delivery correlation bind status 无效")
        expects_winner = self.status in {
            DeliveryCorrelationBindStatus.BOUND,
            DeliveryCorrelationBindStatus.EXISTING,
        }
        if expects_winner != (self.winner is not None):
            raise ValidationError("Delivery correlation bind result 与 winner 不匹配")
        if self.winner is not None and not isinstance(self.winner, MessageAttempt):
            raise ValidationError("Delivery correlation bind winner 类型无效")


@runtime_checkable
class CampaignRepository(Protocol):
    async def add(self, campaign: Campaign, version: CampaignVersion) -> None: ...

    async def get(
        self, tenant_id: TenantId, campaign_id: CampaignId
    ) -> Campaign | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, campaign_id: CampaignId
    ) -> Campaign | None: ...

    async def get_version(
        self, tenant_id: TenantId, campaign_id: CampaignId, version: int
    ) -> CampaignVersion | None: ...

    async def append_version(self, version: CampaignVersion) -> None: ...

    async def update(self, campaign: Campaign) -> None: ...

    async def list_scoped(
        self, tenant_id: TenantId, scope: OutreachScope, limit: int
    ) -> list[Campaign]: ...


@runtime_checkable
class EnrollmentRepository(Protocol):
    async def insert_if_absent(
        self, enrollment: Enrollment
    ) -> EnrollmentInsertResult: ...

    async def get(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> Enrollment | None: ...

    async def get_by_key(
        self, tenant_id: TenantId, key: IdempotencyKey
    ) -> Enrollment | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> Enrollment | None: ...

    async def find_active_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> Enrollment | None: ...

    async def lock_matching_active(
        self, tenant_id: TenantId, target: SuppressionTarget
    ) -> list[Enrollment]: ...

    async def update(self, enrollment: Enrollment) -> None: ...

    async def list_scoped(
        self, tenant_id: TenantId, scope: OutreachScope, limit: int
    ) -> list[Enrollment]: ...

    async def list_due_for_sequence(
        self, tenant_id: TenantId, *, limit: int, now: datetime
    ) -> list[Enrollment]:
        """列出活跃 Campaign 中 ``next_send_at <= now`` 且状态可推进的
        Enrollment（按到期时间排序）。仅供 scheduler 驱动；调用方必须
        先经 service 判权。"""
        ...


@runtime_checkable
class SuppressionRepository(Protocol):
    async def append_if_absent(
        self, entry: SuppressionEntry
    ) -> SuppressionAppendResult: ...

    async def find_current(
        self, tenant_id: TenantId, target: SuppressionTarget
    ) -> SuppressionEntry | None: ...

    async def list_scoped(
        self, tenant_id: TenantId, scope: OutreachScope, limit: int
    ) -> list[SuppressionEntry]: ...


@runtime_checkable
class QuotaRepository(Protocol):
    async def reserve_new_contact(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        on_day: date,
        limit: int,
    ) -> QuotaReservationResult: ...

    async def reserve_message(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        on_day: date,
        limit: int,
    ) -> QuotaReservationResult: ...

    async def get_usage(
        self, tenant_id: TenantId, campaign_id: CampaignId, on_day: date
    ) -> DailyQuotaUsage: ...


@runtime_checkable
class MessageAttemptRepository(Protocol):
    async def create_if_absent(
        self, attempt: MessageAttempt
    ) -> MessageAttemptCreateResult: ...

    async def get_for_update(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId
    ) -> MessageAttempt | None: ...

    async def get_by_key(
        self, tenant_id: TenantId, key: IdempotencyKey
    ) -> MessageAttempt | None: ...

    async def update(self, attempt: MessageAttempt) -> None: ...

    async def bind_delivery_correlation(
        self, attempt: MessageAttempt
    ) -> DeliveryCorrelationBindResult: ...

    async def find_by_deterministic_message_id(
        self, tenant_id: TenantId, deterministic_message_id: str
    ) -> MessageAttempt | None: ...

    async def find_by_idempotency_header(
        self, tenant_id: TenantId, idempotency_header: str
    ) -> MessageAttempt | None: ...


@runtime_checkable
class ActionRepository(Protocol):
    async def append(self, action: ActionRecord) -> bool: ...


@runtime_checkable
class OutreachUnitOfWork(Protocol):
    campaigns: CampaignRepository
    enrollments: EnrollmentRepository
    suppressions: SuppressionRepository
    quotas: QuotaRepository
    attempts: MessageAttemptRepository
    actions: ActionRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type, exc, tb) -> None: ...


@runtime_checkable
class OutreachUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> OutreachUnitOfWork: ...
