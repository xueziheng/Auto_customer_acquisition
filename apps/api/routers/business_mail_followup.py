"""本人普通来信草稿与处理；不含发送入口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.dependencies import get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.routers.business_mail import _actor, _headers
from domains.conversations.business_mail_followup import (
    BusinessMailDraftCommand,
    BusinessMailFollowupConflict,
    BusinessMailFollowupService,
    BusinessMailFollowupView,
    BusinessMailHandlingCommand,
    validate_idempotency_key,
)
from shared.authentication import AuthenticationDenied
from shared.errors import PermissionDenied, TransientError, ValidationError


class FollowupRoute(APIRoute):
    """错误响应不回显正文或凭证输入，所有结果均禁止缓存。"""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request: Request):
            try:
                response = await handler(request)
            except BusinessMailFollowupConflict:
                response = JSONResponse(
                    status_code=409,
                    content={
                        "code": "conflict",
                        "message": "邮件处理记录已变化，请刷新后重新确认",
                        "detail": "邮件处理记录已变化，请刷新后重新确认",
                    },
                )
            except AuthenticationDenied:
                response = JSONResponse(
                    status_code=401,
                    content={
                        "code": "authentication_required",
                        "message": "需要员工身份",
                    },
                )
            except StarletteHTTPException as error:
                code, message = {
                    401: ("authentication_required", "需要员工身份"),
                    403: ("forbidden", "没有权限"),
                }.get(error.status_code, ("http_error", "请求未完成"))
                response = JSONResponse(
                    status_code=error.status_code,
                    content={"code": code, "message": message},
                    headers=error.headers,
                )
            except PermissionDenied:
                response = JSONResponse(
                    status_code=403,
                    content={"code": "forbidden", "message": "没有权限"},
                )
            except (RequestValidationError, ValidationError):
                response = JSONResponse(
                    status_code=400,
                    content={"code": "validation_error", "message": "请求参数无效"},
                )
            except TransientError:
                response = JSONResponse(
                    status_code=503,
                    content={"code": "unavailable", "message": "邮件处理服务暂不可用"},
                )
            _headers(response)
            return response

        return safe


router = APIRouter(
    prefix="/inbox/business-mail", tags=["business-mail"], route_class=FollowupRoute
)


def _service(request: Request) -> BusinessMailFollowupService:
    service = getattr(request.app.state, "business_mail_followup", None)
    if not isinstance(service, BusinessMailFollowupService):
        raise TransientError("普通来信处理服务未配置")
    return service


def _key(request: Request) -> str:
    keys = request.headers.getlist("Idempotency-Key")
    try:
        if len(keys) != 1:
            raise ValueError()
        return validate_idempotency_key(keys[0])
    except ValueError:
        raise ValidationError("请求幂等键无效") from None


@router.get("/{message_id}/followup", response_model=BusinessMailFollowupView)
async def get_followup(
    message_id: str,
    request: Request,
    response: Response,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> BusinessMailFollowupView:
    _headers(response)
    if request.query_params or await request.body():
        raise ValidationError("邮件处理不接受额外参数")
    return await _service(request).get(_actor(identity), message_id)


async def _apply(message_id, command, request, response, identity, action):
    _headers(response)
    if request.query_params:
        raise ValidationError("邮件处理不接受额外参数")
    return await getattr(_service(request), action)(
        _actor(identity), message_id, command, idempotency_key=_key(request)
    )


@router.post("/{message_id}/reply-draft", response_model=BusinessMailFollowupView)
async def save_draft(
    message_id: str,
    body: BusinessMailDraftCommand,
    request: Request,
    response: Response,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> BusinessMailFollowupView:
    return await _apply(message_id, body, request, response, identity, "save_draft")


@router.post("/{message_id}/handling", response_model=BusinessMailFollowupView)
async def handle(
    message_id: str,
    body: BusinessMailHandlingCommand,
    request: Request,
    response: Response,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> BusinessMailFollowupView:
    return await _apply(message_id, body, request, response, identity, "handle")
