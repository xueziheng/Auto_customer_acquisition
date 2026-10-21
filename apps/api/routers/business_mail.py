"""普通业务来信HTTP：身份仅来自认证会话，正文纯文本且禁止缓存。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from domains.conversations.business_mail import (
    BusinessMailActor,
    BusinessMailConflict,
    BusinessMailCorrection,
    BusinessMailDetail,
    BusinessMailPage,
    BusinessMailService,
    MailBucket,
)
from shared.errors import PermissionDenied, TransientError, ValidationError

from ..dependencies import get_request_identity
from ..identity import RequestIdentity

router = APIRouter()


def _service(request: Request) -> BusinessMailService:
    service = getattr(request.app.state, "business_mail", None)
    if not isinstance(service, BusinessMailService):
        raise TransientError("普通业务来信服务未配置")
    return service


def _actor(identity: RequestIdentity) -> BusinessMailActor:
    if identity.employee.user_id is None:
        raise PermissionDenied("普通来信需要当前员工用户身份")
    return BusinessMailActor(
        tenant_id=identity.tenant_id,
        employee_id=identity.employee.employee_id,
        user_id=identity.employee.user_id,
    )


def _headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


@router.get("/inbox/business-mail", response_model=BusinessMailPage)
async def list_business_mail(
    request: Request,
    response: Response,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    bucket: MailBucket = "all",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BusinessMailPage:
    if set(request.query_params) - {"bucket", "offset", "limit"}:
        raise ValidationError("业务来信筛选不接受身份或额外参数")
    _headers(response)
    return await _service(request).list(
        _actor(identity), bucket=bucket, offset=offset, limit=limit
    )


@router.get("/inbox/business-mail/{message_id}", response_model=BusinessMailDetail)
async def detail_business_mail(
    message_id: str,
    request: Request,
    response: Response,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> BusinessMailDetail:
    if request.query_params or await request.body():
        raise ValidationError("业务来信详情不接受额外参数")
    _headers(response)
    return await _service(request).detail(_actor(identity), message_id)


@router.post(
    "/inbox/business-mail/{message_id}/correct-classification",
    response_model=BusinessMailDetail,
    responses={409: {"description": "分类版本已变化"}},
)
async def correct_business_mail(
    message_id: str,
    body: BusinessMailCorrection,
    request: Request,
    response: Response,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> BusinessMailDetail:
    if request.query_params:
        raise ValidationError("业务来信纠正不接受额外参数")
    _headers(response)
    try:
        return await _service(request).correct(_actor(identity), message_id, body)
    except BusinessMailConflict:
        raise HTTPException(
            status_code=409, detail="邮件归类已变化，请刷新后重新确认"
        ) from None
