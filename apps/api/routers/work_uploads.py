"""Work Uploads：原件上传、本人清单、提取对照与最终确认。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from artifact_store.store import RawArtifactKind
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import WorkUploadId
from workflows.employee_work_intake.schemas import (
    ExtractionPayload,
    WorkExtractionView,
    WorkSourceKind,
    WorkUploadView,
)

from ..dependencies import (
    ConfiguredApiDependencies,
    WorkUploadApplicationService,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_UPLOAD_ID = re.compile(r"upl_[0-7][0-9A-HJKMNP-TV-Z]{25}")


def _service(
    dependencies: ConfiguredApiDependencies,
) -> WorkUploadApplicationService:
    if dependencies.work_uploads is None:
        raise TransientError("员工工作上传服务未配置")
    return dependencies.work_uploads


def _upload_id(value: str) -> WorkUploadId:
    if _UPLOAD_ID.fullmatch(value) is None:
        raise ValidationError("员工工作上传标识无效")
    return WorkUploadId(value)


async def _bounded_body(request: Request, maximum_bytes: int) -> bytes:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > maximum_bytes:
            raise ValidationError("员工工作上传原件过大")
    if not content:
        raise ValidationError("员工工作上传原件为空")
    return bytes(content)


@router.get(
    "",
    response_model=list[WorkUploadView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_work_uploads(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WorkUploadView]:
    return await _service(dependencies).list_for_employee(
        identity.tenant_id,
        identity.employee.employee_id,
        limit,
    )


@router.post(
    "",
    status_code=201,
    response_model=WorkUploadView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def create_work_upload(
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    artifact_kind: Annotated[RawArtifactKind, Query()],
    source_kind: Annotated[WorkSourceKind, Query()],
    occurred_at: Annotated[datetime, Query()],
    customer_timezone: Annotated[str, Query(min_length=1, max_length=100)],
) -> WorkUploadView:
    service = _service(dependencies)
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        raise ValidationError("员工工作上传 MIME 无效")
    content = await _bounded_body(request, service.maximum_upload_bytes)
    return await service.create_upload(
        identity.tenant_id,
        identity.employee.employee_id,
        uploaded_by=identity.employee.user_id,
        artifact_kind=artifact_kind,
        source_kind=source_kind,
        content=content,
        mime_type=content_types[0],
        occurred_at=occurred_at,
        customer_timezone=customer_timezone,
    )


@router.get(
    "/{upload_id}/extraction",
    response_model=WorkExtractionView | None,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def get_work_extraction(
    upload_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> WorkExtractionView | None:
    return await _service(dependencies).get_extraction(
        identity.tenant_id,
        _upload_id(upload_id),
        identity.employee.employee_id,
    )


@router.post(
    "/{upload_id}/confirm",
    response_model=WorkExtractionView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def confirm_work_extraction(
    upload_id: str,
    body: dict[str, object],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> WorkExtractionView:
    payload = ExtractionPayload.model_validate(body, strict=False)
    return await _service(dependencies).confirm(
        identity.tenant_id,
        _upload_id(upload_id),
        identity.employee.employee_id,
        payload,
    )


__all__ = ("router",)
