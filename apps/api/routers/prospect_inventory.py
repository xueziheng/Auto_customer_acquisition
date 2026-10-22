"""公共候选交付HTTP边界：会话身份、公司归属与安全状态投影。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from domains.opportunities.permissions import ScopeLevel
from domains.prospecting.inventory import (
    InventoryAssignmentRequest,
    InventoryTransferRequest,
)
from domains.prospecting.inventory_followup import InventoryFollowupScope
from domains.prospecting.service import (
    AcquisitionJob,
    CandidateAllocation,
    InventoryQuery,
    InventoryService,
    InventoryStatus,
    PublicCandidate,
    SourceState,
)
from shared.errors import IdempotencyConflict, PermissionDenied, TransientError
from shared.schemas.discovery_sources import DISCOVERY_SOURCES
from shared.schemas.identifiers import (
    AcquisitionJobId,
    CandidateAllocationId,
    EmployeeId,
    ProspectAccountId,
    TenantId,
)
from workflows.enterprise_knowledge.confirmed_context import ConfirmedContextReader

from ..dependencies import (
    document_idempotency_header,
    get_api_dependencies,
    get_request_identity,
    raw_idempotency_key,
)
from ..identity import RequestIdentity

router = APIRouter(prefix="/inventory", tags=["prospect-inventory"])
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]


class InventoryRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: InventoryQuery
    count: int = Field(strict=True, ge=1, le=100)
    followup_scope: InventoryFollowupScope | None = None


class InventoryAllocationView(BaseModel):
    """只含已授权归属与公开事实；不公开内部幂等键。"""

    id: str
    employee_id: EmployeeId | None
    candidate: PublicCandidate
    allocated_at: datetime
    assigned_at: datetime | None = None
    assigned_by: EmployeeId | None = None
    account_id: ProspectAccountId | None = None
    email_verification: Literal["unverified"] = "unverified"


class InventoryJobView(BaseModel):
    """任务租约、并发令牌和底层异常永不进入HTTP。"""

    id: str
    employee_id: EmployeeId
    query: InventoryQuery
    requested_count: int
    remaining_count: int
    status: Literal["queued", "processing", "complete", "failed"]
    attempts: int
    failure_reason: (
        Literal["sources_unavailable", "budget_exhausted", "collector_failed"] | None
    ) = None
    created_at: datetime
    updated_at: datetime


class InventoryRequestView(BaseModel):
    allocations: tuple[InventoryAllocationView, ...]
    job: InventoryJobView | None


class InventorySourceView(BaseModel):
    source_id: str
    name: str
    category: str
    access_methods: tuple[str, ...]
    website_url: str | None
    state: SourceState


class InventorySourcesReader(Protocol):
    """只交付真实运行能力及历史结果，不读凭证到响应。"""

    async def list_sources(self, tenant_id: TenantId) -> tuple[SourceState, ...]: ...


def authorized_owners(identity: RequestIdentity) -> tuple[EmployeeId, ...] | None:
    """只使用当前会话已经由员工域推导的作用域。"""
    if (
        not identity.employee.is_active
        or identity.employee.tenant_id != identity.tenant_id
    ):
        raise PermissionDenied("候选访问被拒绝")
    role, scope = identity.employee.role, identity.opportunity_actor.scope
    if role == "boss" and scope.level is ScopeLevel.TENANT:
        return None
    if role in {"manager", "sales"}:
        return (identity.employee.employee_id,)
    raise PermissionDenied("候选访问被拒绝")


def require_boss(identity: RequestIdentity) -> None:
    """公司搜索与分配必须由当前老板会话发起，服务门面还会重新读取。"""
    if authorized_owners(identity) is not None:
        raise PermissionDenied("仅企业管理员可搜索、分配或读取公司任务")


def _service(request: Request, identity: RequestIdentity) -> InventoryService:
    value = getattr(request.app.state, "prospect_inventory_factory", None)
    if value is None:
        raise TransientError("候选库存服务尚未配置")
    return cast(InventoryService, value(identity))


def _allocation(value: CandidateAllocation) -> InventoryAllocationView:
    return InventoryAllocationView(
        id=str(value.id),
        employee_id=value.employee_id,
        candidate=value.candidate,
        allocated_at=value.allocated_at,
        assigned_at=value.assigned_at,
        assigned_by=value.assigned_by,
        account_id=value.account_id,
    )


def _job(value: AcquisitionJob) -> InventoryJobView:
    return InventoryJobView(
        id=str(value.id),
        employee_id=value.employee_id,
        query=value.query,
        requested_count=value.requested_count,
        remaining_count=value.remaining_count,
        status=value.status,
        attempts=value.attempts,
        failure_reason=value.failure_reason,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


@router.get("/status", response_model=InventoryStatus)
async def status(request: Request, identity: Identity) -> InventoryStatus:
    require_boss(identity)
    return await _service(request, identity).status(identity.tenant_id)


@router.post("/requests", response_model=InventoryRequestView, status_code=202)
async def request_candidates(
    body: InventoryRequestBody,
    request: Request,
    identity: Identity,
    _key: Annotated[None, Depends(document_idempotency_header)],
) -> InventoryRequestView:
    require_boss(identity)
    if body.query.confirmed_refs:
        knowledge = get_api_dependencies(request).enterprise_knowledge
        if knowledge is None:
            raise TransientError("企业资料服务尚未配置")
        await ConfirmedContextReader(knowledge.service).read(
            identity.tenant_id, identity.employee.employee_id, body.query.confirmed_refs
        )
    try:
        result = await _service(request, identity).request(
            identity.tenant_id,
            identity.employee.employee_id,
            body.query,
            body.count,
            raw_idempotency_key(request),
            **({"followup_scope": body.followup_scope} if body.followup_scope is not None else {}),
        )
    except IdempotencyConflict:
        raise HTTPException(status_code=409) from None
    return InventoryRequestView(
        allocations=tuple(_allocation(a) for a in result.allocations),
        job=_job(result.job) if result.job else None,
    )


@router.post("/assignments", response_model=tuple[InventoryAllocationView, ...])
async def assign_allocations(
    body: InventoryAssignmentRequest, request: Request, identity: Identity
) -> tuple[InventoryAllocationView, ...]:
    require_boss(identity)
    try:
        values = await _service(request, identity).assign(
            identity.tenant_id,
            body.allocation_ids,
            body.employee_id,
            acting_employee_id=identity.employee.employee_id,
        )
    except IdempotencyConflict:
        raise HTTPException(status_code=409, detail="客户已有其他负责人") from None
    return tuple(_allocation(value) for value in values)


@router.post(
    "/allocations/{allocation_id}/transfer", response_model=InventoryAllocationView
)
async def transfer_allocation(
    allocation_id: str,
    body: InventoryTransferRequest,
    request: Request,
    identity: Identity,
) -> InventoryAllocationView:
    require_boss(identity)
    try:
        value = await _service(request, identity).transfer(
            identity.tenant_id,
            CandidateAllocationId(allocation_id),
            body,
            acting_employee_id=identity.employee.employee_id,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409, detail="归属已改变、跟进仍活动或发送待核对，请刷新核对"
        ) from None
    return _allocation(value)


@router.get("/allocations", response_model=tuple[InventoryAllocationView, ...])
async def allocations(
    request: Request,
    identity: Identity,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    unassigned_only: bool = False,
) -> tuple[InventoryAllocationView, ...]:
    owners = authorized_owners(identity)
    if unassigned_only:
        require_boss(identity)
    try:
        values = await _service(request, identity).list_allocations(
            identity.tenant_id,
            owners,
            limit,
            cursor,
            **({"unassigned_only": True} if unassigned_only else {}),
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的历史游标") from None
    return tuple(_allocation(value) for value in values)


@router.get("/jobs", response_model=tuple[InventoryJobView, ...])
async def jobs(
    request: Request,
    identity: Identity,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> tuple[InventoryJobView, ...]:
    require_boss(identity)
    values = await _service(request, identity).list_jobs(
        identity.tenant_id, None, limit
    )
    return tuple(_job(value) for value in values)


@router.get("/jobs/{job_id}", response_model=InventoryJobView)
async def job(job_id: str, request: Request, identity: Identity) -> InventoryJobView:
    require_boss(identity)
    value = await _service(request, identity).get_job(
        identity.tenant_id, AcquisitionJobId(job_id)
    )
    if value is None:
        raise HTTPException(status_code=404)
    return _job(value)


@router.get("/sources", response_model=tuple[InventorySourceView, ...])
async def sources(
    request: Request, identity: Identity
) -> tuple[InventorySourceView, ...]:
    require_boss(identity)
    await _service(request, identity).status(identity.tenant_id)
    reader = cast(
        InventorySourcesReader | None,
        getattr(request.app.state, "inventory_sources", None),
    )
    states = (
        {s.source_id: s for s in await reader.list_sources(identity.tenant_id)}
        if reader
        else {}
    )
    return tuple(
        InventorySourceView(
            source_id=s.source_id,
            name=s.name,
            category=s.category,
            access_methods=s.access_methods,
            website_url=s.website_url,
            state=states.get(s.source_id)
            or SourceState(
                source_id=s.source_id,
                availability="not_integrated",
                cost_class="unknown",
                relevant=False,
            ),
        )
        for s in DISCOVERY_SOURCES
        if s.selection == "selected"
    )
