"""Campaign Center。

GET  /campaigns                  列表 + 今日用量/上限
POST /campaigns                  创建（边界校验，发件身份只能选
                                 list_available_for_campaign 的结果）
POST /campaigns/{id}/start       老板一次确认并启动自动发送
POST /campaigns/{id}/submit      提交审批
POST /campaigns/{id}/pause       暂停（只停新发送，回复处理不停）
POST /campaigns/{id}/revise      修改边界 = 新版本重新审批
GET  /campaigns/{id}/enrollments 序列进度
GET  /sending-identities         发件身份状态（认证/预热/信誉/熔断）
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from domains.approvals.service import ApprovalType, BlastRadius
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
from domains.outreach.schemas import (
    CampaignCreateRequest,
    CampaignView,
    EnrollmentView,
    MessageAttemptView,
    SequenceStepRequest,
    StepIntent,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    EnrollmentId,
    TenantId,
    UserId,
)
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
_CAMPAIGN_WRITER_ROLES = frozenset({"boss", "manager"})
_BOSS_ROLES = frozenset({"boss"})


class CampaignSequenceStepBody(BaseModel):
    """Campaign 的单个邮件步骤；第一步 discovery 由域边界校验。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    step_number: int = Field(ge=1, le=5)
    intent: Literal["discovery", "presentation", "follow_up"]
    wait_days: int = Field(ge=0, le=90)


class CampaignBoundaryBody(BaseModel):
    """完整 Campaign 边界；修改必须提交全量新版本。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str
    markets: list[str]
    target_entity_types: list[str]
    allowed_categories: list[str]
    sender_identity_ids: list[str]
    steps: list[CampaignSequenceStepBody]
    daily_new_contact_limit: int = Field(ge=1)
    daily_total_message_limit: int = Field(ge=1)
    handoff_triggers: list[str]
    stop_on_reply: bool = True


class CampaignPauseBody(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    reason: str


class CampaignSubmitResponse(BaseModel):
    """提交审批后的 Campaign 与不可变审批包引用。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    campaign: CampaignView
    approval_id: str


def _campaign_id(value: str) -> CampaignId:
    if re.fullmatch(r"cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}", value) is None:
        raise ValidationError("campaign id 无效")
    return CampaignId(value)


def _campaign_request(body: CampaignBoundaryBody) -> CampaignCreateRequest:
    return CampaignCreateRequest(
        name=body.name,
        markets=tuple(body.markets),
        target_entity_types=tuple(body.target_entity_types),
        allowed_categories=tuple(body.allowed_categories),
        sender_identity_ids=tuple(body.sender_identity_ids),  # type: ignore[arg-type]
        steps=tuple(
            SequenceStepRequest(
                step_number=item.step_number,
                intent=StepIntent(item.intent),
                wait_days=item.wait_days,
            )
            for item in body.steps
        ),
        daily_new_contact_limit=body.daily_new_contact_limit,
        daily_total_message_limit=body.daily_total_message_limit,
        handoff_triggers=tuple(body.handoff_triggers),
        stop_on_reply=body.stop_on_reply,
    )


def _approval_change(campaign: CampaignView) -> dict[str, object]:
    boundary = campaign.boundary
    return {
        "campaign_id": str(campaign.campaign_id),
        "version": campaign.version,
        "name": campaign.name,
        "markets": list(boundary.markets),
        "target_entity_types": list(boundary.target_entity_types),
        "allowed_categories": list(boundary.allowed_categories),
        "sender_identity_ids": [str(value) for value in boundary.sender_identity_ids],
        "sequence": [
            {
                "step_number": item.step_number,
                "intent": item.intent.value,
                "wait_days": item.wait_days,
            }
            for item in boundary.steps
        ],
        "daily_new_contact_limit": boundary.daily_new_contact_limit,
        "daily_total_message_limit": boundary.daily_total_message_limit,
        "handoff_triggers": list(boundary.handoff_triggers),
        "stop_on_reply": boundary.stop_on_reply,
    }


async def _submit_boundary_approval(
    campaign: CampaignView,
    identity: RequestIdentity,
    dependencies: ConfiguredApiDependencies,
    *,
    inline_authorization: bool = False,
) -> ApprovalId:
    if dependencies.approvals is None:
        raise ValidationError("审批服务未配置")
    return await dependencies.approvals.submit(
        identity.tenant_id,
        ApprovalType.CAMPAIGN_BOUNDARY_CHANGE,
        f"批准 Campaign：{campaign.name}（版本 {campaign.version}）",
        _approval_change(campaign),
        "Campaign 边界创建或修订后必须由人工确认，批准仅对该精确版本有效。",
        BlastRadius(
            affected_entities=[
                f"Campaign {campaign.campaign_id} 版本 {campaign.version}"
            ],
            if_approved="允许老板激活该精确版本，并在列明的市场、品类、序列和每日上限内运行。",
            if_rejected="Campaign 保持待审批，不会产生新的自动发送。",
            reversible=True,
        ),
        proposed_by_employee=(
            None if inline_authorization else identity.employee.employee_id
        ),
        change_set_ref=f"campaign:{campaign.campaign_id}:v{campaign.version}",
        owner_employee=None if inline_authorization else campaign.created_by,
    )


@router.get(
    "/campaigns",
    response_model=list[CampaignView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_campaigns(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CampaignView]:
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_LIST,
        allowed_roles=_OUTREACH_ROLES,
    )
    return await dependencies.outreach.list_campaigns(
        identity.tenant_id, actor.scope, limit=limit, actor=actor
    )


@router.get(
    "/campaigns/{campaign_id}",
    response_model=CampaignView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def get_campaign(
    campaign_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignView:
    typed_id = _campaign_id(campaign_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_READ,
        allowed_roles=_OUTREACH_ROLES,
    )
    return await dependencies.outreach.get_campaign(
        identity.tenant_id, typed_id, actor=actor
    )


@router.post(
    "/campaigns",
    response_model=CampaignView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def create_campaign(
    body: CampaignBoundaryBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignView:
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_CREATE,
        allowed_roles=_BOSS_ROLES,
    )
    return await dependencies.outreach.create_campaign(
        identity.tenant_id, _campaign_request(body), actor=actor
    )


@router.post(
    "/campaigns/{campaign_id}/submit",
    response_model=CampaignSubmitResponse,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def submit_campaign(
    campaign_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignSubmitResponse:
    typed_id = _campaign_id(campaign_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_SUBMIT,
        allowed_roles=_CAMPAIGN_WRITER_ROLES,
    )
    campaign = await dependencies.outreach.submit_campaign(
        identity.tenant_id, typed_id, actor=actor
    )
    approval_id = await _submit_boundary_approval(
        campaign,
        identity,
        dependencies,
        inline_authorization=True,
    )
    return CampaignSubmitResponse(campaign=campaign, approval_id=str(approval_id))


@router.post(
    "/campaigns/{campaign_id}/start",
    response_model=CampaignView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def start_campaign(
    campaign_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignView:
    """把精确边界确认、留痕和启动合并成老板的一次操作。"""

    typed_id = _campaign_id(campaign_id)
    submit_actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_SUBMIT,
        allowed_roles=_BOSS_ROLES,
    )
    campaign = await dependencies.outreach.submit_campaign(
        identity.tenant_id, typed_id, actor=submit_actor
    )
    if dependencies.approvals is None:
        raise ValidationError("审批服务未配置")
    approval_id = await _submit_boundary_approval(
        campaign,
        identity,
        dependencies,
        inline_authorization=True,
    )
    await dependencies.approvals.decide(
        identity.tenant_id,
        approval_id,
        True,
        identity.employee.employee_id,
        "老板在活动中心一次确认并启动自动发邮件",
    )
    activate_actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_ACTIVATE,
        allowed_roles=_BOSS_ROLES,
    )
    activated = await dependencies.outreach.activate_campaign(
        identity.tenant_id, typed_id, actor=activate_actor
    )
    await dependencies.approvals.mark_applied(
        identity.tenant_id,
        approval_id,
        f"campaign-activate:{activated.campaign_id}:v{activated.version}",
    )
    return activated


@router.post(
    "/campaigns/{campaign_id}/revise",
    response_model=CampaignSubmitResponse,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def revise_campaign(
    campaign_id: str,
    body: CampaignBoundaryBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignSubmitResponse:
    typed_id = _campaign_id(campaign_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_REVISE,
        allowed_roles=_CAMPAIGN_WRITER_ROLES,
    )
    campaign = await dependencies.outreach.revise_campaign(
        identity.tenant_id, typed_id, _campaign_request(body), actor=actor
    )
    approval_id = await _submit_boundary_approval(
        campaign,
        identity,
        dependencies,
        inline_authorization=True,
    )
    return CampaignSubmitResponse(campaign=campaign, approval_id=str(approval_id))


@router.post(
    "/campaigns/{campaign_id}/activate",
    response_model=CampaignView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def activate_campaign(
    campaign_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignView:
    typed_id = _campaign_id(campaign_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_ACTIVATE,
        allowed_roles=_BOSS_ROLES,
    )
    campaign = await dependencies.outreach.activate_campaign(
        identity.tenant_id, typed_id, actor=actor
    )
    if campaign.approval_id is not None and dependencies.approvals is not None:
        change_set_ref = f"campaign:{campaign.campaign_id}:v{campaign.version}"
        approval = await dependencies.approvals.get_by_change_set(
            identity.tenant_id, change_set_ref
        )
        if approval is not None and approval.approval_id == campaign.approval_id:
            await dependencies.approvals.mark_applied(
                identity.tenant_id,
                campaign.approval_id,
                f"campaign-activate:{campaign.campaign_id}:v{campaign.version}",
            )
    return campaign


@router.post(
    "/campaigns/{campaign_id}/pause",
    response_model=CampaignView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def pause_campaign(
    campaign_id: str,
    body: CampaignPauseBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignView:
    typed_id = _campaign_id(campaign_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_PAUSE,
        allowed_roles=_CAMPAIGN_WRITER_ROLES,
    )
    return await dependencies.outreach.pause_campaign(
        identity.tenant_id, typed_id, body.reason, actor=actor
    )


@router.post(
    "/campaigns/{campaign_id}/cancel",
    response_model=CampaignView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def cancel_campaign(
    campaign_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CampaignView:
    typed_id = _campaign_id(campaign_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.CAMPAIGN_CANCEL,
        allowed_roles=_CAMPAIGN_WRITER_ROLES,
    )
    return await dependencies.outreach.cancel_campaign(
        identity.tenant_id, typed_id, actor=actor
    )


@router.get(
    "/campaigns/{campaign_id}/enrollments",
    response_model=list[EnrollmentView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_campaign_enrollments(
    campaign_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> list[EnrollmentView]:
    typed_id = _campaign_id(campaign_id)
    actor = await resolve_outreach_access(
        identity,
        dependencies,
        OutreachAction.ENROLLMENT_LIST,
        allowed_roles=_OUTREACH_ROLES,
    )
    await dependencies.outreach.get_campaign(identity.tenant_id, typed_id, actor=actor)
    rows = await dependencies.outreach.list_enrollments(
        identity.tenant_id, actor.scope, limit=limit, actor=actor
    )
    return [row for row in rows if row.campaign_id == typed_id]


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
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
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
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
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
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
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
