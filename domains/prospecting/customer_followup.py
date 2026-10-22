"""已分配客户的安全投影与当前归属读取端口；不等同于触达授权。"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from domains.prospecting.inventory import CandidateAllocation, PublicCandidate
from domains.prospecting.schemas import ProspectAccountView, ProspectContactDetailView
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    CandidateAllocationId,
    EmployeeId,
    ProspectAccountId,
    TenantId,
    UserId,
)

CustomerFollowupMode = Literal["automatic", "manual"]


class CustomerAccessView(BaseModel):
    role: Literal["boss", "manager", "sales"]
    can_search: bool
    can_assign: bool


class CustomerAllocationView(BaseModel):
    id: CandidateAllocationId
    employee_id: EmployeeId | None
    candidate: PublicCandidate
    allocated_at: datetime
    assigned_at: datetime | None
    assigned_by: EmployeeId | None
    account_id: ProspectAccountId | None
    email_verification: Literal["unverified"] = "unverified"

    @classmethod
    def from_allocation(cls, value: CandidateAllocation) -> "CustomerAllocationView":
        """裁剪内部请求键，保留真实公共来源。"""
        return cls(**value.model_dump(exclude={"tenant_id", "request_key"}))


class CustomerEnrollmentView(BaseModel):
    enrollment_id: str
    campaign_id: str
    campaign_version: int
    account_id: str
    contact_point_id: str
    state: str
    current_step: int
    next_send_at: datetime | None
    enrolled_at: datetime
    stopped_at: datetime | None
    stop_reason: str | None
    source_hypothesis_id: str | None
    execution_mode: CustomerFollowupMode = "automatic"


class CustomerHypothesisOption(BaseModel):
    hypothesis_id: str
    category: str
    status: str
    is_inference: Literal[True] = True


class CustomerCampaignOption(BaseModel):
    campaign_id: str
    name: str
    campaign_version: int
    markets: tuple[str, ...]
    target_entity_types: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    eligible_contact_point_ids: tuple[str, ...] = ()
    can_start: bool = False


class CustomerFollowupOptions(BaseModel):
    allocation_id: str
    account_id: str | None
    contacts: tuple[ProspectContactDetailView, ...]
    hypotheses: tuple[CustomerHypothesisOption, ...]
    campaigns: tuple[CustomerCampaignOption, ...]
    blocking_reasons: tuple[str, ...]
    execution_mode: CustomerFollowupMode = "automatic"


class CustomerDetailView(BaseModel):
    allocation: CustomerAllocationView
    account: ProspectAccountView | None
    owner: EmployeeId | None
    contacts: tuple[ProspectContactDetailView, ...]
    enrollments: tuple[CustomerEnrollmentView, ...]
    can_stop: bool = False
    blocking_reasons: tuple[str, ...]


class CustomerFollowupCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    campaign_id: str = Field(pattern=r"^cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    contact_point_id: str = Field(pattern=r"^cp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    source_hypothesis_id: str = Field(pattern=r"^hyp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    campaign_version: int = Field(ge=1)
    execution_mode: CustomerFollowupMode = "automatic"


@dataclass(frozen=True)
class CustomerContext:
    tenant_id: TenantId
    employee_id: EmployeeId
    user_id: UserId | None
    role: str
    is_active: bool
    allocation: CandidateAllocation | None
    ownership_owner: EmployeeId | None

    def require_identity(
        self, tenant_id: TenantId, employee_id: EmployeeId, user_id: UserId | None
    ) -> None:
        """对组合端口返回身份再次求交，不把存储快照当额外授权。"""
        if (
            self.tenant_id != tenant_id
            or self.employee_id != employee_id
            or self.user_id != user_id
            or not self.is_active
            or self.role not in {"boss", "manager", "sales"}
        ):
            raise PermissionDenied("客户访问身份已改变")

    def require(
        self, *, expected_user_id: UserId | None, own: bool
    ) -> CandidateAllocation:
        """同公司在职身份与当前真实归属求交；boss 仅详情可看他人。"""
        allocation = self.allocation
        if (
            not self.is_active
            or self.role not in {"boss", "manager", "sales"}
            or self.user_id != expected_user_id
            or allocation is None
            or allocation.tenant_id != self.tenant_id
            or (
                (own or self.role != "boss")
                and allocation.employee_id != self.employee_id
            )
        ):
            raise PermissionDenied("客户不属于当前员工或身份已改变")
        if (
            self.role != "boss"
            and allocation.account_id is not None
            and self.ownership_owner != self.employee_id
        ):
            raise PermissionDenied("客户当前归属已改变")
        if own and (
            allocation.account_id is None or self.ownership_owner != self.employee_id
        ):
            raise PermissionDenied("客户档案或当前归属尚未同步")
        return allocation


class CustomerContextReader(Protocol):
    def guard(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        allocation_id: CandidateAllocationId | None,
    ) -> AbstractAsyncContextManager[CustomerContext]:
        """按员工→取得记录→归属锁顺序持锁至调用结束。"""
        ...

    async def active_campaign_ids(self, tenant_id: TenantId) -> tuple[str, ...]:
        """仅返回本公司真实active ID，不授予 Campaign 配置权。"""
        ...
