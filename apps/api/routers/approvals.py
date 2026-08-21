"""Approval Center。

GET  /approvals/pending          我的待审批（按过期时间升序）
GET  /approvals/{id}             审批包全文（目标：不跳页面即可决定）
POST /approvals/{id}/decide      批准/否决（自批禁止在服务层强制）
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from domains.approvals.schemas import ApprovalView
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import ApprovalId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_APPROVAL_ID_RE = re.compile(r"apr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_APPROVER_ROLES = frozenset({"boss", "manager"})


class ApprovalDecisionBody(BaseModel):
    """审批决定只有批准或拒绝；不存在 force/override。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str | None = None


def _require_approver(identity: RequestIdentity) -> None:
    if identity.employee.role not in _APPROVER_ROLES:
        raise PermissionDenied("当前角色无权审批")


def _approval_service(dependencies: ConfiguredApiDependencies):
    if dependencies.approvals is None:
        raise TransientError("审批服务未配置")
    return dependencies.approvals


def _approval_id(value: str) -> ApprovalId:
    if _APPROVAL_ID_RE.fullmatch(value) is None:
        raise ValidationError("approval id 无效")
    return ApprovalId(value)


@router.get(
    "/approvals/pending",
    response_model=list[ApprovalView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_pending_approvals(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ApprovalView]:
    _require_approver(identity)
    return await _approval_service(dependencies).list_pending_for(
        identity.tenant_id, identity.employee.employee_id, limit
    )


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def get_approval(
    approval_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ApprovalView:
    _require_approver(identity)
    return await _approval_service(dependencies).get(
        identity.tenant_id,
        _approval_id(approval_id),
        current_employee=identity.employee.employee_id,
    )


@router.post(
    "/approvals/{approval_id}/decide",
    response_model=ApprovalView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def decide_approval(
    approval_id: str,
    body: ApprovalDecisionBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ApprovalView:
    _require_approver(identity)
    typed_id = _approval_id(approval_id)
    if body.decision == "reject" and (body.reason is None or not body.reason.strip()):
        raise ValidationError("拒绝审批必须填写原因")
    service = _approval_service(dependencies)
    await service.decide(
        identity.tenant_id,
        typed_id,
        body.decision == "approve",
        identity.employee.employee_id,
        body.reason,
    )
    return await service.get(
        identity.tenant_id,
        typed_id,
        current_employee=identity.employee.employee_id,
    )
