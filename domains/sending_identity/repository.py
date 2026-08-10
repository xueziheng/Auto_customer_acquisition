"""发件身份存储 Protocol（内部契约，不暴露 ORM）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.sending_identity.models import (
    DomainRole,
    IdentityState,
    ReputationWindow,
    SendingIdentity,
)
from domains.sending_identity.permissions import (
    SendingIdentityAction,
    SendingIdentityScope,
)
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DeliveryEventRecord,
    SendReservation,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, TenantId


@dataclass(frozen=True)
class SendingDomain:
    tenant_id: TenantId
    domain: str
    role: DomainRole
    created_at: datetime


@dataclass(frozen=True)
class AuthenticationCheckRecord:
    auth_check_id: str
    tenant_id: TenantId
    identity_id: SendingIdentityId
    result: AuthenticationResult
    created_at: datetime


@dataclass(frozen=True)
class IdentityRegistrationResult:
    """按地址原子登记的 typed winner。"""

    created: bool
    winner: SendingIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.created, bool) or not isinstance(self.winner, SendingIdentity):
            raise TypeError("identity registration result 无效")


@dataclass(frozen=True)
class AuthenticationAppendResult:
    """按 check_ref 原子追加的 typed winner。"""

    created: bool
    winner: AuthenticationCheckRecord

    def __post_init__(self) -> None:
        if not isinstance(self.created, bool) or not isinstance(
            self.winner, AuthenticationCheckRecord
        ):
            raise TypeError("authentication append result 无效")


@dataclass(frozen=True)
class IdentityActionRecord:
    action_id: str
    tenant_id: TenantId
    identity_id: SendingIdentityId
    action_key: str
    action: SendingIdentityAction
    before_state: IdentityState | None
    after_state: IdentityState | None
    actor_id: str
    scope: str
    rule: str
    note: str | None
    occurred_at: datetime


class ReservationOutcome(str, Enum):
    CREATED = "created"
    EXISTING = "existing"
    CAP_REACHED = "cap_reached"


@dataclass(frozen=True)
class ReservationResult:
    outcome: ReservationOutcome
    reservation: SendReservation | None
    sent_attempts: int

    def __post_init__(self) -> None:
        if (self.outcome is ReservationOutcome.CAP_REACHED) != (self.reservation is None):
            raise ValueError("reservation outcome 与 reservation 不一致")


@runtime_checkable
class SendingDomainRepository(Protocol):
    async def add(self, domain: SendingDomain) -> None: ...

    async def get(self, tenant_id: TenantId, domain: str) -> SendingDomain | None: ...

    async def ensure(self, domain: SendingDomain) -> SendingDomain: ...


@runtime_checkable
class SendingIdentityRepository(Protocol):
    async def add(self, identity: SendingIdentity) -> None: ...

    async def register_if_address_absent(
        self, identity: SendingIdentity
    ) -> IdentityRegistrationResult: ...

    async def get(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, for_update: bool = False
    ) -> SendingIdentity | None: ...

    async def update(self, identity: SendingIdentity) -> None: ...

    async def find_by_address(self, tenant_id: TenantId, address: str) -> SendingIdentity | None: ...

    async def list_domain_for_update(self, tenant_id: TenantId, domain: str) -> list[SendingIdentity]: ...

    async def find_domain_role(self, tenant_id: TenantId, domain: str) -> DomainRole | None: ...

    async def list_available_for_campaign(
        self, tenant_id: TenantId, scope: SendingIdentityScope, limit: int
    ) -> list[SendingIdentity]: ...


@runtime_checkable
class AuthenticationCheckRepository(Protocol):
    async def add(self, record: AuthenticationCheckRecord) -> None: ...

    async def append_if_ref_absent(
        self, record: AuthenticationCheckRecord
    ) -> AuthenticationAppendResult: ...

    async def latest_for_identity(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> AuthenticationCheckRecord | None: ...


@runtime_checkable
class ReputationRepository(Protocol):
    async def record_event(self, event: DeliveryEventRecord) -> bool: ...

    async def compute_window(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, window_days: int, computed_at: datetime
    ) -> ReputationWindow: ...

    async def compute_domain_window(
        self, tenant_id: TenantId, domain: str, window_days: int, computed_at: datetime
    ) -> ReputationWindow: ...


@runtime_checkable
class SendCounterRepository(Protocol):
    async def get_count(self, tenant_id: TenantId, identity_id: SendingIdentityId, on_day: date) -> int: ...


@runtime_checkable
class SendReservationRepository(Protocol):
    async def reserve_if_below(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reservation_key: IdempotencyKey,
        on_day: date,
        daily_limit: int,
        created_at: datetime,
    ) -> ReservationResult: ...


@runtime_checkable
class IdentityActionRepository(Protocol):
    async def add(self, record: IdentityActionRecord) -> None: ...

    async def exists_by_key(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, action_key: str
    ) -> bool: ...


@runtime_checkable
class SendingIdentityUnitOfWork(Protocol):
    domains: SendingDomainRepository
    identities: SendingIdentityRepository
    auth_checks: AuthenticationCheckRepository
    reputation: ReputationRepository
    counters: SendCounterRepository
    reservations: SendReservationRepository
    actions: IdentityActionRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class SendingIdentityUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> SendingIdentityUnitOfWork: ...
