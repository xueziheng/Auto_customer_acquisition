"""模型调用的技术配额契约和确定性计量，不决定贸易业务授权。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from shared.schemas.identifiers import ModelInvocationId, TenantId, UserId
from shared.schemas.model_invocation import InvocationIdentity, ModelLimits, ModelUsage

InvocationState = Literal[
    "reserved", "dispatched", "succeeded", "rejected", "invalid", "unknown"
]


class Reservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: ModelInvocationId | None
    outcome: Literal["reserved", "duplicate", "conflict", "limited"]
    state: InvocationState | None


class InvocationView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: ModelInvocationId
    identity: InvocationIdentity
    model: str
    state: InvocationState
    usage: ModelUsage
    created_at: datetime
    finished_at: datetime | None
    slot_released: bool


class ModelUsageRepository(Protocol):
    async def reserve(
        self,
        identity: InvocationIdentity,
        request_hmac: str,
        limits: ModelLimits,
        now: datetime,
        *,
        model: str,
    ) -> Reservation: ...
    async def mark_dispatched(
        self, tenant_id: TenantId, invocation_id: ModelInvocationId
    ) -> None: ...
    async def finish(
        self,
        tenant_id: TenantId,
        invocation_id: ModelInvocationId,
        usage: ModelUsage,
        state: InvocationState,
    ) -> None: ...
    async def get(
        self, tenant_id: TenantId, invocation_id: ModelInvocationId
    ) -> InvocationView: ...
    async def release_unknown_slot(
        self,
        tenant_id: TenantId,
        invocation_id: ModelInvocationId,
        operator: UserId,
        reason: str,
    ) -> None: ...


def window_start(now: datetime, seconds: int) -> datetime:
    """固定 UTC 技术窗口；不以本机时区重置余额。"""
    if now.tzinfo is None or type(seconds) is not int or seconds <= 0:
        raise ValueError("配额窗口无效")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (now.astimezone(UTC) - epoch) // timedelta(seconds=1)
    return epoch + timedelta(seconds=(elapsed // seconds) * seconds)


def model_cost(
    usage: ModelUsage,
    input_rate: Decimal | None,
    cached_rate: Decimal | None,
    output_rate: Decimal | None,
) -> Decimal | None:
    """每百万 token 的可信费率；任一计量/费率未知就不编造最终金额。"""
    for rate in (input_rate, cached_rate, output_rate):
        if rate is not None and (
            not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0
        ):
            raise ValueError("模型费率必须为非负 Decimal")
    if (
        usage.input_tokens is None
        or usage.cached_input_tokens is None
        or usage.output_tokens is None
        or input_rate is None
        or output_rate is None
        or (usage.cached_input_tokens > 0 and cached_rate is None)
    ):
        return None
    return (
        (usage.input_tokens - usage.cached_input_tokens) * input_rate
        + usage.cached_input_tokens * (cached_rate or Decimal(0))
        + usage.output_tokens * output_rate
    ) / Decimal(1_000_000)
