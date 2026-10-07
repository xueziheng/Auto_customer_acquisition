"""免费搜索的持久额度端口与确定性安全策略，不依赖具体数据库。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from connectors.search_contracts import SearchCostStatus, SearchUsage
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

ReservationStatus = Literal["reserved", "uncertain", "consumed"]


class FreeSearchStopReason(str, Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    USAGE_UNKNOWN = "usage_unknown"
    PAID_ENABLED = "paid_enabled"
    REQUEST_UNCERTAIN = "request_uncertain"
    UNSUPPORTED = "unsupported"


class FreeSearchError(ToolGatewayError):
    """稳定业务停止原因；Gateway 内仍使用既有安全技术分类。"""

    def __init__(self, reason: FreeSearchStopReason) -> None:
        super().__init__(
            ToolErrorCategory.RECONCILIATION_REQUIRED
            if reason is FreeSearchStopReason.REQUEST_UNCERTAIN
            else ToolErrorCategory.PROVIDER_PERMANENT
        )
        self.reason = reason
        self.is_retryable = False


@dataclass(frozen=True)
class SearchQuotaRunState:
    tenant_id: TenantId
    run_id: RunId
    stop_reason: FreeSearchStopReason | None
    updated_at: datetime


@dataclass(frozen=True)
class SearchQuotaSnapshot:
    """受信部署的单账户槽；不是 Provider 返回的账户身份，不含凭证引用。"""

    tenant_id: TenantId
    provider: Literal["tavily"]
    remaining: int
    reservations: int
    cost_status: SearchCostStatus
    usage_limit: int | None
    usage_used: int | None
    paygo_enabled: bool | None
    checked_at: datetime | None
    included_credits_free: bool = False

    def __post_init__(self) -> None:
        if type(self.included_credits_free) is not bool or (
            self.included_credits_free
            and (
                not isinstance(self.cost_status, SearchCostStatus)
                or self.cost_status is SearchCostStatus.PAID
                or (self.paygo_enabled is not None and self.paygo_enabled is not False)
                or any(
                    type(value) is not int or value < 0
                    for value in (self.usage_limit, self.usage_used)
                )
            )
        ):
            raise ValidationError("免费搜索套餐资格快照无效")


@dataclass(frozen=True)
class SearchReservation:
    """只含安全操作标识；请求正文和 Provider payload 不进入额度表。"""

    tenant_id: TenantId
    run_id: RunId
    request_key: str
    status: ReservationStatus
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class SearchQuotaRepository(Protocol):
    """构造时绑定受信租户和唯一部署账户槽，禁止逐请求传账户别名。"""

    async def check_available(
        self, run_id: RunId, request_key: str, *, fingerprint_version: str
    ) -> None: ...

    async def reserve(
        self, run_id: RunId, request_key: str, usage: SearchUsage,
        *, fingerprint_version: str,
    ) -> SearchReservation: ...

    async def record_unavailable(self, run_id: RunId) -> None: ...

    async def mark_dispatched(self, run_id: RunId, request_key: str) -> None: ...

    async def consume(self, run_id: RunId, request_key: str) -> None: ...

    async def acknowledge_uncertain_as_consumed(
        self, run_id: RunId, request_key: str
    ) -> None:
        """人工核对后仅收紧 uncertain→consumed；精确 consumed 重放为 no-op。"""
        ...

    async def snapshot(self) -> SearchQuotaSnapshot | None: ...

    async def get(
        self, run_id: RunId, request_key: str
    ) -> SearchReservation | None: ...

    async def run_state(self, run_id: RunId) -> SearchQuotaRunState | None: ...


def verified_free_remaining(usage: SearchUsage) -> int | None:
    """精确套餐须有独立免费额度证明或旧明确免费状态；未知字段不回填。"""
    if (
        not isinstance(usage, SearchUsage)
        or usage.plan != "Researcher"
        or usage.cost_status is SearchCostStatus.PAID
        or usage.paygo_enabled is True
        or usage.limit is None
        or usage.used is None
        or not (
            usage.included_credits_free
            or (
                usage.cost_status is SearchCostStatus.FREE
                and usage.paygo_enabled is False
            )
        )
    ):
        return None
    return max(0, usage.limit - usage.used)
