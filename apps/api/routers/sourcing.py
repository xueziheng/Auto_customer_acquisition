"""Sourcing Center 的受限 HTTP 编排；业务读取与二次授权均留在寻源服务。"""

from __future__ import annotations

import re
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import (
    PublicSourcingPlanCommand,
    PublicSourcingPlanReadView,
    SourcingAdmissionReadView,
    SourcingCandidateReadView,
    SourcingCaseReadView,
    SourcingCurrentQuotaReadView,
    SourcingLadderCheckReadView,
    SourcingReviewCommand,
    SourcingReviewReadView,
    SourcingUncertainExecutionReadView,
    SourcingUncertainReconciliationCommand,
)
from domains.sourcing.service import AdmissionState, SourcingService
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import (
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPlanId,
)
from workflows.sourcing_case.application import (
    SourcingAdmissionApplication,
    SourcingAdmissionDetailView,
    SourcingAdmissionListView,
    SourcingCaseApplication,
)

from ..dependencies import (
    ConfiguredApiDependencies,
    document_idempotency_header,
    get_api_dependencies,
    get_request_identity,
    raw_idempotency_key,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_CASE_ID = re.compile(r"src_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ADMISSION_ID = re.compile(r"sad_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_READ_ROLES = frozenset({"boss", "product", "sourcing", "finance"})
_PLAN_DRAFT_ROLES = frozenset({"boss", "sourcing"})
_BOSS_ONLY = frozenset({"boss"})
_REVIEW_ROLES = frozenset({"boss", "product", "sourcing"})
_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorResponse},
    403: {"model": ApiErrorResponse},
    404: {"model": ApiErrorResponse},
    409: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}


class PlanReferenceBody(BaseModel):
    """确认/运行只接受已保存计划的不可变标识和精确哈希。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    plan_id: SourcingPlanId
    expected_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _case_id(value: str) -> SourcingCaseId:
    if _CASE_ID.fullmatch(value) is None:
        raise ValidationError("寻源案例标识无效")
    return SourcingCaseId(value)


def _actor(
    identity: RequestIdentity, *, allowed_roles: frozenset[str]
) -> SourcingActor:
    """只从经过员工域验证的 RequestIdentity 构造 actor，禁止请求自报权限。"""

    if identity.employee.role not in allowed_roles:
        raise PermissionDenied("API role gate 默认拒绝")
    return SourcingActor(
        actor_id=str(identity.employee.employee_id),
        tenant_id=identity.tenant_id,
        scope=SourcingScope.TENANT,
        role=identity.employee.role,
    )


def _sourcing(dependencies: ConfiguredApiDependencies) -> SourcingService:
    if dependencies.sourcing is None:
        raise TransientError("寻源服务尚未配置")
    return dependencies.sourcing


def _application(dependencies: ConfiguredApiDependencies) -> SourcingCaseApplication:
    if dependencies.sourcing_application is None:
        raise TransientError("寻源编排服务尚未配置")
    return dependencies.sourcing_application


def _admission_application(
    dependencies: ConfiguredApiDependencies,
) -> SourcingAdmissionApplication:
    application = getattr(dependencies, "sourcing_admission_application", None)
    if application is None:
        raise TransientError("寻源准入编排服务尚未配置")
    return application


def _admission_id(value: str) -> SourcingAdmissionId:
    if _ADMISSION_ID.fullmatch(value) is None:
        raise ValidationError("寻源准入标识无效")
    return SourcingAdmissionId(value)


def _not_found(value: object) -> object:
    if value is None:
        raise HTTPException(status_code=404)
    return value


@router.get(
    "/sourcing-admissions",
    response_model=SourcingAdmissionListView,
    responses=_ERRORS,
)
async def list_sourcing_admissions(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    state: AdmissionState = AdmissionState.WAITING,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> SourcingAdmissionListView:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    return await _admission_application(dependencies).list_read_view(
        identity.tenant_id, state=state.value, limit=limit, actor=actor
    )


@router.get(
    "/sourcing-admissions/{admission_id}",
    response_model=SourcingAdmissionDetailView,
    responses=_ERRORS,
)
async def get_sourcing_admission(
    admission_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> SourcingAdmissionDetailView:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    result = await _admission_application(dependencies).get_read_view(
        identity.tenant_id, _admission_id(admission_id), actor=actor
    )
    return cast(SourcingAdmissionDetailView, _not_found(result))


@router.post(
    "/sourcing-admissions/{admission_id}/admit",
    response_model=SourcingAdmissionReadView,
    responses=_ERRORS,
    dependencies=[Depends(document_idempotency_header)],
)
async def manually_admit_sourcing_case(
    admission_id: str,
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> SourcingAdmissionReadView:
    actor = _actor(identity, allowed_roles=frozenset({"boss", "sourcing"}))
    request_id = raw_idempotency_key(request)
    result = await _admission_application(dependencies).admit_one(
        identity.tenant_id,
        _admission_id(admission_id),
        request_id=request_id,
        actor=actor,
    )
    return cast(SourcingAdmissionReadView, _not_found(result))


@router.get(
    "/sourcing-cases", response_model=list[SourcingCaseReadView], responses=_ERRORS
)
async def list_cases(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> list[SourcingCaseReadView]:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    return list(
        await _sourcing(dependencies).list_case_read_views(
            identity.tenant_id, actor=actor, limit=limit
        )
    )


@router.get(
    "/sourcing-cases/{case_id}",
    response_model=SourcingCaseReadView,
    responses=_ERRORS,
)
async def get_case(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> SourcingCaseReadView:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    result = await _sourcing(dependencies).get_case_read_view(
        identity.tenant_id, _case_id(case_id), actor=actor
    )
    return cast(SourcingCaseReadView, _not_found(result))


@router.get(
    "/sourcing-cases/{case_id}/ladder-checks",
    response_model=list[SourcingLadderCheckReadView],
    responses=_ERRORS,
)
async def get_ladder_checks(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> list[SourcingLadderCheckReadView]:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    result = await _sourcing(dependencies).get_ladder_check_read_views(
        identity.tenant_id, _case_id(case_id), actor=actor
    )
    return list(cast(tuple[SourcingLadderCheckReadView, ...], _not_found(result)))


@router.get(
    "/sourcing-cases/{case_id}/candidates",
    response_model=list[SourcingCandidateReadView],
    responses=_ERRORS,
)
async def get_candidates(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> list[SourcingCandidateReadView]:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    result = await _sourcing(dependencies).get_candidate_read_views(
        identity.tenant_id, _case_id(case_id), actor=actor, limit=limit
    )
    return list(cast(tuple[SourcingCandidateReadView, ...], _not_found(result)))


@router.get(
    "/sourcing-cases/{case_id}/public-search-plan",
    response_model=PublicSourcingPlanReadView | None,
    responses=_ERRORS,
)
async def get_public_search_plan(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> PublicSourcingPlanReadView | None:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    normalized_case_id = _case_id(case_id)
    case = await _sourcing(dependencies).get_case_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )
    _not_found(case)
    return await _sourcing(dependencies).get_public_plan_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )


@router.get(
    "/sourcing-cases/{case_id}/current-quota",
    response_model=SourcingCurrentQuotaReadView,
    responses=_ERRORS,
)
async def get_current_quota(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> SourcingCurrentQuotaReadView:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    result = await _application(dependencies).get_current_quota_read_view(
        identity.tenant_id, _case_id(case_id), actor=actor
    )
    return cast(SourcingCurrentQuotaReadView, _not_found(result))


@router.get(
    "/sourcing-cases/{case_id}/review",
    response_model=SourcingReviewReadView | None,
    responses=_ERRORS,
)
async def get_review(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> SourcingReviewReadView | None:
    actor = _actor(identity, allowed_roles=_REVIEW_ROLES)
    normalized_case_id = _case_id(case_id)
    case = await _sourcing(dependencies).get_case_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )
    _not_found(case)
    return await _sourcing(dependencies).get_review_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )


@router.get(
    "/sourcing-cases/{case_id}/uncertain-reconciliations",
    response_model=list[SourcingUncertainExecutionReadView],
    responses=_ERRORS,
)
async def list_uncertain_reconciliations(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> list[SourcingUncertainExecutionReadView]:
    actor = _actor(identity, allowed_roles=_READ_ROLES)
    result = await _application(dependencies).list_uncertain_execution_read_views(
        identity.tenant_id, _case_id(case_id), actor=actor, limit=limit
    )
    return list(
        cast(tuple[SourcingUncertainExecutionReadView, ...], _not_found(result))
    )


@router.post(
    "/sourcing-cases/{case_id}/public-search-plan",
    response_model=PublicSourcingPlanReadView,
    responses=_ERRORS,
)
async def create_public_search_plan(
    case_id: str,
    command: PublicSourcingPlanCommand,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> PublicSourcingPlanReadView:
    actor = _actor(identity, allowed_roles=_PLAN_DRAFT_ROLES)
    normalized_case_id = _case_id(case_id)
    if command.case_id != normalized_case_id:
        raise ValidationError("公开寻源计划 case_id 必须匹配路径")
    await _application(dependencies).create_plan(
        identity.tenant_id, normalized_case_id, command, actor=actor
    )
    result = await _sourcing(dependencies).get_public_plan_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )
    return cast(PublicSourcingPlanReadView, _not_found(result))


@router.post(
    "/sourcing-cases/{case_id}/public-search-plan/confirm",
    response_model=PublicSourcingPlanReadView,
    responses=_ERRORS,
    dependencies=[Depends(document_idempotency_header)],
)
async def confirm_public_search_plan(
    case_id: str,
    body: PlanReferenceBody,
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> PublicSourcingPlanReadView:
    actor = _actor(identity, allowed_roles=_BOSS_ONLY)
    raw_idempotency_key(request)
    normalized_case_id = _case_id(case_id)
    await _application(dependencies).confirm_plan(
        identity.tenant_id,
        normalized_case_id,
        body.plan_id,
        body.expected_plan_hash,
        actor=actor,
    )
    result = await _sourcing(dependencies).get_public_plan_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )
    return cast(PublicSourcingPlanReadView, _not_found(result))


@router.post(
    "/sourcing-cases/{case_id}/run",
    response_model=PublicSourcingPlanReadView,
    responses=_ERRORS,
    dependencies=[Depends(document_idempotency_header)],
)
async def run_public_search_plan(
    case_id: str,
    body: PlanReferenceBody,
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> PublicSourcingPlanReadView:
    actor = _actor(identity, allowed_roles=_BOSS_ONLY)
    raw_idempotency_key(request)
    normalized_case_id = _case_id(case_id)
    await _application(dependencies).run(
        identity.tenant_id,
        normalized_case_id,
        body.plan_id,
        body.expected_plan_hash,
        actor=actor,
    )
    result = await _sourcing(dependencies).get_public_plan_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )
    return cast(PublicSourcingPlanReadView, _not_found(result))


@router.post(
    "/sourcing-cases/{case_id}/review",
    response_model=SourcingReviewReadView,
    responses=_ERRORS,
    dependencies=[Depends(document_idempotency_header)],
)
async def submit_review(
    case_id: str,
    command: SourcingReviewCommand,
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> SourcingReviewReadView:
    actor = _actor(identity, allowed_roles=_REVIEW_ROLES)
    request_id = raw_idempotency_key(request)
    normalized_case_id = _case_id(case_id)
    await _application(dependencies).review(
        identity.tenant_id,
        normalized_case_id,
        command,
        request_id=request_id,
        actor=actor,
    )
    result = await _sourcing(dependencies).get_review_read_view(
        identity.tenant_id, normalized_case_id, actor=actor
    )
    return cast(SourcingReviewReadView, _not_found(result))


@router.post(
    "/sourcing-cases/{case_id}/reconcile-uncertain-request",
    responses=_ERRORS,
    dependencies=[Depends(document_idempotency_header)],
)
async def reconcile_uncertain_request(
    case_id: str,
    command: SourcingUncertainReconciliationCommand,
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> None:
    actor = _actor(identity, allowed_roles=_BOSS_ONLY)
    raw_idempotency_key(request)
    await _application(dependencies).reconcile_uncertain(
        identity.tenant_id, _case_id(case_id), command, actor=actor
    )


__all__ = ("router",)
