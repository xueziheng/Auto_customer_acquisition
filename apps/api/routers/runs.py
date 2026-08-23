"""Run Center：老板只读的 Workflow Run 安全审计投影。"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import RunId
from workflows.engine.audit import (
    RunAuditActor,
    RunAuditService,
    RunDetailView,
    RunSummaryView,
)

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_RUN_ID = re.compile(r"run_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_WORKFLOW_TYPE = r"^[a-z][a-z0-9_]{0,63}$"
_RUN_STATUS = (
    r"^(pending|running|completed|failed|waiting_human|waiting_event|"
    r"timed_out|cancelled)$"
)


def _service(dependencies: ConfiguredApiDependencies) -> RunAuditService:
    if dependencies.run_audit is None:
        raise TransientError("Run 审计服务未配置")
    return dependencies.run_audit


def _actor(identity: RequestIdentity) -> RunAuditActor:
    """第一道角色门；服务层 authorizer 仍会进行第二次 tenant-bound 判权。"""
    if identity.employee.role != "boss":
        raise PermissionDenied("Run 审计仅允许老板读取")
    return RunAuditActor(
        actor_id=str(identity.employee.employee_id),
        role=identity.employee.role,
    )


def _run_id(value: str) -> RunId:
    if _RUN_ID.fullmatch(value) is None:
        raise ValidationError("Run 标识无效")
    return RunId(value)


@router.get(
    "",
    response_model=list[RunSummaryView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_runs(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    workflow_type: Annotated[str | None, Query(pattern=_WORKFLOW_TYPE)] = None,
    status: Annotated[str | None, Query(pattern=_RUN_STATUS)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[RunSummaryView]:
    return await _service(dependencies).list_runs(
        identity.tenant_id,
        actor=_actor(identity),
        workflow_type=workflow_type,
        status=status,
        limit=limit,
    )


@router.get(
    "/{run_id}",
    response_model=RunDetailView,
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
        404: {"model": ApiErrorResponse},
    },
)
async def get_run(
    run_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> RunDetailView:
    result = await _service(dependencies).get_run(
        identity.tenant_id,
        _run_id(run_id),
        actor=_actor(identity),
    )
    if result is None:
        raise HTTPException(status_code=404)
    return result


__all__ = ("router",)
