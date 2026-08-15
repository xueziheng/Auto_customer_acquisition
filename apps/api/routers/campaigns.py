"""Campaign Center。

GET  /campaigns                  列表 + 今日用量/上限
POST /campaigns                  创建（边界校验，发件身份只能选
                                 list_available_for_campaign 的结果）
POST /campaigns/{id}/submit      提交审批
POST /campaigns/{id}/pause       暂停（只停新发送，回复处理不停）
POST /campaigns/{id}/revise      修改边界 = 新版本重新审批
GET  /campaigns/{id}/enrollments 序列进度
GET  /sending-identities         发件身份状态（认证/预热/信誉/熔断）
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachAction,
    OutreachScope,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.schemas import EnrollmentView, MessageAttemptView
from shared.errors import ValidationError
from shared.schemas.identifiers import EnrollmentId, TenantId, UserId
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.pipeline import ToolCallContext, ToolCallResult

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_api_settings,
    get_request_identity,
    resolve_outreach_access,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse, ApiSettings

router = APIRouter()

_ATTEMPT_ID_RE = re.compile(r"mat_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ENROLLMENT_ID_RE = re.compile(r"enr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_OUTREACH_ROLES = frozenset({"boss", "manager", "sales"})


class ManualEmailSendBody(BaseModel):
    """员工唯一可提交的邮件内容；所有资源绑定均由服务端解析。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    subject: str
    body: str


class ManualEmailSendResponse(BaseModel):
    """不包含地址、正文、密钥或 URL 的安全发送结果。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tool_call_id: str | None
    status: str
    duplicate: bool
    provider_ref: str | None
    error_category: str | None
    retry_after_seconds: int | None = None


def _error(
    status_code: int,
    *,
    code: str,
    message: str,
    retry_after: int | None = None,
) -> JSONResponse:
    headers = None
    if retry_after is not None:
        headers = {"Retry-After": str(min(max(retry_after, 1), 86_400))}
    return JSONResponse(
        status_code=status_code,
        content=ApiErrorResponse(code=code, message=message).model_dump(mode="json"),
        headers=headers,
    )


def _map_failure(
    result: ToolCallResult,
    *,
    fallback_retry_after: int,
) -> JSONResponse:
    category = result.error_category or ToolErrorCategory.UNEXPECTED
    if category is ToolErrorCategory.VALIDATION:
        return _error(400, code="validation_error", message="发送请求无效")
    if category is ToolErrorCategory.APPROVAL_REQUIRED:
        return _error(400, code="approval_required", message="客户内容需要人工审批")
    if category is ToolErrorCategory.PERMISSION_DENIED:
        return _error(403, code="forbidden", message="没有权限")
    if category is ToolErrorCategory.SUPPRESSED:
        return _error(403, code="suppressed", message="当前事实不允许发送")
    if category is ToolErrorCategory.IDEMPOTENCY_CONFLICT:
        return _error(
            409,
            code="idempotency_conflict",
            message="发送请求与既有记录冲突",
        )
    if category is ToolErrorCategory.IN_PROGRESS:
        return _error(409, code="in_progress", message="发送正在处理中")
    if category is ToolErrorCategory.RECONCILIATION_REQUIRED:
        return _error(
            409,
            code="reconciliation_required",
            message="发送结果需要人工对账",
        )
    if category is ToolErrorCategory.RATE_LIMITED:
        return _error(
            429,
            code="rate_limited",
            message="发送额度暂不可用",
            retry_after=result.retry_after_seconds or fallback_retry_after,
        )
    if category in {
        ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
        ToolErrorCategory.UNEXPECTED,
    }:
        return _error(
            503,
            code="service_unavailable",
            message="发送服务暂不可用",
            retry_after=result.retry_after_seconds or fallback_retry_after,
        )
    return _error(400, code="request_rejected", message="发送请求被服务拒绝")


@router.post(
    "/message-attempts/{attempt_id}/send",
    response_model=ManualEmailSendResponse,
    response_model_exclude={"retry_after_seconds"},
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
        409: {"model": ApiErrorResponse},
        429: {"model": ApiErrorResponse},
        503: {"model": ApiErrorResponse},
    },
)
async def send_manual_email(
    attempt_id: str,
    body: ManualEmailSendBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
) -> ManualEmailSendResponse | JSONResponse:
    """以当前租户与员工身份发起唯一的受保护 Gmail Gateway 调用。"""
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise ValidationError("message attempt id 无效")
    result = await dependencies.tool_gateway.invoke(
        ToolCallContext(
            tenant_id=TenantId(identity.tenant_id),
            user_id=UserId(str(identity.employee.employee_id)),
            tool_id="email.send",
            params={
                "attempt_id": attempt_id,
                "subject": body.subject,
                "body": body.body,
            },
        )
    )
    if result.status not in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
        return _map_failure(
            result,
            fallback_retry_after=settings.retry_after_seconds,
        )
    output = result.output or {}
    provider_ref = output.get("provider_ref")
    return ManualEmailSendResponse(
        tool_call_id=result.tool_call_id,
        status=result.status.value,
        duplicate=result.status is ToolCallStatus.DUPLICATE,
        provider_ref=provider_ref if isinstance(provider_ref, str) else None,
        error_category=None,
    )


@router.get(
    "/enrollments",
    response_model=list[EnrollmentView],
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
    },
)
async def list_enrollments(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[EnrollmentView]:
    """按员工作用域列出可见 Enrollment；scope 由身份与 Campaign 归属推导。"""
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.ENROLLMENT_LIST,
        allowed_roles=_OUTREACH_ROLES,
    )
    return await dependencies.outreach.list_enrollments(
        identity.tenant_id,
        actor.scope,
        limit=limit,
        actor=actor,
    )


@router.post(
    "/enrollments/{enrollment_id}/attempts/prepare",
    response_model=MessageAttemptView,
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
    },
)
async def prepare_message_attempt(
    enrollment_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> MessageAttemptView:
    """先以员工 scope 验证 Enrollment 归属，再用精确 SYSTEM scope 准备 Attempt。

    域内 ENROLLMENT_PREPARE_SEND 只允许 SYSTEM actor；归属验证走
    ENROLLMENT_READ（域 authorizer 以真实 campaign/account/enrollment 判权）。
    """
    if _ENROLLMENT_ID_RE.fullmatch(enrollment_id) is None:
        raise ValidationError("enrollment id 无效")
    typed_id = EnrollmentId(enrollment_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.ENROLLMENT_READ,
        allowed_roles=_OUTREACH_ROLES,
    )
    await dependencies.outreach.get_enrollment(
        identity.tenant_id, typed_id, actor=actor
    )
    system_actor = OutreachActor(
        "system:api-prepare-attempt",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({typed_id}),
        ),
        "system",
    )
    return await dependencies.outreach.prepare_message_attempt(
        identity.tenant_id, typed_id, actor=system_actor
    )
