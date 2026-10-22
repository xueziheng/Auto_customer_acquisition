"""本人客户详情与批准边界内跟进动作；身份与归属只由服务端解析。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from domains.prospecting.customer_followup import (
    CustomerAccessView,
    CustomerAllocationView,
    CustomerDetailView,
    CustomerEnrollmentView,
    CustomerFollowupCommand,
    CustomerFollowupMode,
    CustomerFollowupOptions,
)
from shared.errors import TransientError
from shared.schemas.identifiers import CandidateAllocationId, EnrollmentId

from ..composition.customer_followup import CustomerFollowupApplication
from ..dependencies import (
    ConfiguredApiDependencies,
    document_idempotency_header,
    get_api_dependencies,
    get_request_identity,
    raw_idempotency_key,
)
from ..identity import RequestIdentity
from .prospect_inventory import _service as inventory_service

router = APIRouter(prefix="/customers", tags=["customers"])
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]
Dependencies = Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)]


def _application(
    dependencies: ConfiguredApiDependencies,
) -> CustomerFollowupApplication:
    if dependencies.customer_followup is None:
        raise TransientError("客户跟进服务尚未配置")
    return dependencies.customer_followup


@router.get("/access", response_model=CustomerAccessView)
async def access(identity: Identity, dependencies: Dependencies) -> CustomerAccessView:
    return await _application(dependencies).access(identity)


@router.get("", response_model=tuple[CustomerAllocationView, ...])
async def customers(
    request: Request,
    identity: Identity,
    dependencies: Dependencies,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> tuple[CustomerAllocationView, ...]:
    await _application(dependencies).access(identity)
    try:
        values = await inventory_service(request, identity).list_allocations(
            identity.tenant_id, (identity.employee.employee_id,), limit, cursor
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的历史游标") from None
    return tuple(CustomerAllocationView.from_allocation(v) for v in values)


@router.get("/{allocation_id}", response_model=CustomerDetailView)
async def detail(
    allocation_id: str, identity: Identity, dependencies: Dependencies
) -> CustomerDetailView:
    return await _application(dependencies).detail(
        identity, CandidateAllocationId(allocation_id)
    )


@router.get("/{allocation_id}/follow-up", response_model=CustomerFollowupOptions)
async def options(
    allocation_id: str,
    identity: Identity,
    dependencies: Dependencies,
    execution_mode: CustomerFollowupMode = "automatic",
) -> CustomerFollowupOptions:
    return await _application(dependencies).options(
        identity, CandidateAllocationId(allocation_id), execution_mode=execution_mode
    )


@router.post("/{allocation_id}/follow-up", response_model=CustomerEnrollmentView)
async def start(
    allocation_id: str,
    body: CustomerFollowupCommand,
    request: Request,
    identity: Identity,
    dependencies: Dependencies,
    _key: Annotated[None, Depends(document_idempotency_header)],
) -> CustomerEnrollmentView:
    return await _application(dependencies).start(
        identity,
        CandidateAllocationId(allocation_id),
        body,
        raw_idempotency_key(request),
    )


@router.post(
    "/{allocation_id}/follow-up/{enrollment_id}/stop",
    response_model=CustomerEnrollmentView,
)
async def stop(
    allocation_id: str,
    enrollment_id: str,
    identity: Identity,
    dependencies: Dependencies,
) -> CustomerEnrollmentView:
    return await _application(dependencies).stop(
        identity, CandidateAllocationId(allocation_id), EnrollmentId(enrollment_id)
    )
