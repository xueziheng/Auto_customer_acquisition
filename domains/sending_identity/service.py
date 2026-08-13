"""发件身份域公共服务契约。

上层只能通过此模块消费 typed DTO 与服务 Protocol。实现的当前日期/时间必须
来自注入时钟；任何公共方法均不接受调用方日期或绕过预热的参数。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.sending_identity.models import DeliveryEventType
from domains.sending_identity.permissions import Actor
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DeliveryEventRecord,
    DomainReputationView,
    IdentityRegisterRequest,
    IdentityView,
    ReputationView,
    SendPermission,
    SendReservation,
    WarmupProgressView,
)
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, TenantId

__all__ = [
    "Actor",
    "AuthenticationResult",
    "DeliveryEventRecord",
    "DeliveryEventType",
    "DomainReputationView",
    "IdentityRegisterRequest",
    "IdentityView",
    "ReputationView",
    "SendPermission",
    "SendReservation",
    "SendingIdentityService",
    "WarmupProgressView",
]


@runtime_checkable
class SendingIdentityService(Protocol):
    """发件身份公共 API；所有方法均要求显式 typed actor。"""

    async def register(
        self, tenant_id: TenantId, request: IdentityRegisterRequest, *, actor: Actor
    ) -> SendingIdentityId: ...

    async def begin_authentication(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> None: ...

    async def record_authentication_result(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        result: AuthenticationResult,
        *,
        actor: Actor,
    ) -> None: ...

    async def start_warmup(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        target_daily_volume: int,
        *,
        actor: Actor,
    ) -> None: ...

    async def advance_warmup(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> None: ...

    async def check_send_permission(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        for_cold_outreach: bool,
        *,
        actor: Actor,
    ) -> SendPermission: ...

    async def reserve_send_slot(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reservation_key: IdempotencyKey,
        for_cold_outreach: bool,
        *,
        actor: Actor,
    ) -> SendReservation: ...

    async def record_delivery_event(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        event: DeliveryEventRecord,
        *,
        actor: Actor,
    ) -> bool: ...

    async def evaluate_reputation(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> ReputationView: ...

    async def resume_from_throttle(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> None: ...

    async def resume_from_suspension(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        investigation_note: str,
        *,
        actor: Actor,
    ) -> None: ...

    async def retire(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reason: str,
        *,
        actor: Actor,
    ) -> None: ...

    async def get(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> IdentityView: ...

    async def list_available_for_campaign(
        self, tenant_id: TenantId, *, limit: int, actor: Actor
    ) -> list[IdentityView]: ...

    async def get_domain_reputation(
        self, tenant_id: TenantId, domain: str, *, actor: Actor
    ) -> DomainReputationView: ...

    async def get_warmup_progress(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> WarmupProgressView: ...
