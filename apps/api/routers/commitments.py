"""Commitment Center：本人承诺查询、确认与履约。"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict

from domains.commitments.schemas import CommitmentView
from domains.commitments.service import CommitmentService
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import CommitmentId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_COMMITMENT_ID = re.compile(r"com_[0-7][0-9A-HJKMNP-TV-Z]{25}")


class CommitmentConfirmBody(BaseModel):
    """员工可确认原提取，或提供带时区的绝对到期时间修正。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    corrected_due_at: str | None = None


def _service(dependencies: ConfiguredApiDependencies) -> CommitmentService:
    if dependencies.commitments is None:
        raise TransientError("承诺服务未配置")
    return dependencies.commitments


def _id(value: str) -> CommitmentId:
    if _COMMITMENT_ID.fullmatch(value) is None:
        raise ValidationError("承诺标识无效")
    return CommitmentId(value)


@router.get(
    "",
    response_model=list[CommitmentView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_commitments(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    include_fulfilled: Annotated[bool, Query()] = False,
) -> list[CommitmentView]:
    return await _service(dependencies).list_for_employee(
        identity.tenant_id,
        identity.employee.employee_id,
        include_fulfilled,
    )


@router.get(
    "/overdue",
    response_model=list[CommitmentView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_overdue_commitments(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> list[CommitmentView]:
    return await _service(dependencies).list_overdue_for_employee(
        identity.tenant_id, identity.employee.employee_id
    )


@router.post(
    "/{commitment_id}/confirm",
    status_code=204,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def confirm_commitment(
    commitment_id: str,
    body: CommitmentConfirmBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> Response:
    await _service(dependencies).confirm(
        identity.tenant_id,
        _id(commitment_id),
        identity.employee.employee_id,
        body.corrected_due_at,
    )
    return Response(status_code=204)


@router.post(
    "/{commitment_id}/fulfill",
    status_code=204,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def fulfill_commitment(
    commitment_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> Response:
    await _service(dependencies).fulfill(
        identity.tenant_id,
        _id(commitment_id),
        identity.employee.employee_id,
    )
    return Response(status_code=204)
