"""三档获客服务的持久配置与工作流端口；不包含支付或发送授权。"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Literal, Protocol, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from domains.prospecting.inventory import InventoryQuery
from domains.prospecting.service_tiers import (
    ScreeningEvidence,
    ScreeningEvidenceKind,
    ServiceTier,
    ServiceTierPolicy,
)
from shared.schemas.identifiers import ProspectAccountId, TenantId


def _identity(value: str | None) -> str | None:
    if value is not None and (
        not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("服务计划标识必须非空且不含空白或控制字符")
    return value


class ServicePlanState(BaseModel):
    """当前服务期的保守预留；未决消耗不会因切档或重启清零。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    tenant_id: TenantId = Field(min_length=1, max_length=64)
    tier: ServiceTier
    version: int = Field(strict=True, ge=1)
    period_started_at: datetime
    period_ends_at: datetime
    candidates_reserved: int = Field(strict=True, ge=0)
    searches_reserved: int = Field(strict=True, ge=0)
    contacts_reserved: int = Field(strict=True, ge=0)
    schedule_enabled: bool = Field(strict=True)
    target_kind: Literal["legacy_research", "inventory_request"] = "legacy_research"
    query: InventoryQuery | None = None
    employee_id: str | None = None
    proposal_id: str | None = None
    acting_user_id: str | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None

    _identities = field_validator("tenant_id", "proposal_id", "acting_user_id")(
        _identity
    )

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        """缺少身份与明确时区的排程不得隐式取得执行权。"""
        for value in (
            self.period_started_at,
            self.period_ends_at,
            self.next_run_at,
            self.last_run_at,
        ):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("服务计划时间必须带时区")
        if self.period_ends_at <= self.period_started_at:
            raise ValueError("服务期结束必须晚于开始")
        if self.schedule_enabled and not (
            self.acting_user_id and self.next_run_at and (
                (self.target_kind == "legacy_research" and self.proposal_id) or
                (self.target_kind == "inventory_request" and self.query and self.employee_id)
            )
        ):
            raise ValueError("周期探索必须绑定确认提案、操作者与下次运行时间")
        return self


class PendingDiscoveryCycle(BaseModel):
    """启动回执不确定时仍重用同一批次身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    key: str
    target_kind: Literal["legacy_research", "inventory_request"] = "legacy_research"
    query: InventoryQuery | None = None
    employee_id: str | None = None
    proposal_id: str | None = None
    acting_user_id: str

    _identities = field_validator("tenant_id", "key", "proposal_id", "acting_user_id")(
        _identity
    )


class ServicePlanReservation(BaseModel):
    """当前服务期的不可扩张预留回执；供长流程恢复时重读。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str
    tier: ServiceTier
    created_at: datetime
    period_started_at: datetime
    candidates: int = Field(strict=True, ge=0)
    searches: int = Field(strict=True, ge=0)
    contacts: int = Field(strict=True, ge=0)


class ProspectingServicePlanStore(Protocol):
    """所有读写显式绑定租户；实现负责事务、CAS、幂等与保守预留。"""

    async def get(self, tenant_id: TenantId) -> ServicePlanState | None: ...

    async def assign(
        self,
        tenant_id: TenantId,
        tier: ServiceTier,
        *,
        expected_version: int | None,
        actor_id: str,
        now: datetime,
    ) -> ServicePlanState: ...

    async def configure_schedule(
        self,
        tenant_id: TenantId,
        *,
        proposal_id: str | None,
        acting_user_id: str | None,
        enabled: bool,
        expected_version: int,
        now: datetime,
    ) -> ServicePlanState: ...

    async def configure_inventory_schedule(
        self, tenant_id: TenantId, *, query: InventoryQuery, employee_id: str,
        acting_user_id: str, enabled: bool, expected_version: int, now: datetime,
    ) -> ServicePlanState: ...

    async def bind_inventory_cycle(
        self, tenant_id: TenantId, *, key: str, request_key_hash: str, now: datetime,
    ) -> ServicePlanState: ...

    async def reserve(
        self,
        tenant_id: TenantId,
        *,
        key: str,
        candidates: int,
        searches: int,
        contacts: int,
        now: datetime,
    ) -> None: ...

    async def reservation(
        self, tenant_id: TenantId, *, key: str, now: datetime
    ) -> ServicePlanReservation | None: ...

    async def begin_cycle(
        self, tenant_id: TenantId, now: datetime
    ) -> PendingDiscoveryCycle | None: ...

    async def claim_cycle_start(
        self, tenant_id: TenantId, *, key: str, expected_version: int, now: datetime
    ) -> bool:
        """前置授权检查后原子取得启动权；未知回执保留该标记，不能换身份。"""
        ...

    async def bind_cycle(
        self, tenant_id: TenantId, *, key: str, run_id: str, now: datetime
    ) -> ServicePlanState: ...

    async def get_evidence(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> tuple[ScreeningEvidence, ...]: ...

    async def append_evidence(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        evidence: ScreeningEvidence,
    ) -> None: ...


def contact_mission_key(campaign_id: str, account_id: str) -> str:
    """同客户同Campaign为同一次联系人处理；新旧准备路径复用保守额度。"""
    _identity(campaign_id)
    _identity(account_id)
    return "contacts:" + sha256(f"{campaign_id}\0{account_id}".encode()).hexdigest()


class ServicePlanStatusView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    catalog: list[ServiceTierPolicy]
    plan: ServicePlanState | None


class ServicePlanScheduleBody(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    query: InventoryQuery | None = None
    proposal_id: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool
    expected_version: int = Field(ge=1)


class ScreeningEvidenceBody(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    kind: ScreeningEvidenceKind
    signal_id: str = Field(min_length=1, max_length=64)
    # FastAPI首先把JSON解成dict；datetime字段需接受ISO字符串而非strict Python对象。
    occurred_at: AwareDatetime | None = Field(default=None, strict=False)
