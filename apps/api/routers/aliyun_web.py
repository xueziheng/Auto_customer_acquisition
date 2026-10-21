"""当前登录员工的阿里邮箱连接；密码只进入受信认证路径。"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import SecretStr

from apps.api.composition.aliyun_web import AliyunWebService
from apps.api.dependencies import get_request_identity
from apps.api.identity import RequestIdentity
from domains.conversations.aliyun_connection import (
    AliyunActor,
    AliyunConnectCommand,
    AliyunConnectionStatus,
)
from shared.authentication import AuthPrincipal

router = APIRouter(prefix="/inbox/aliyun", tags=["private-aliyun"])
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]


def _actor(request: Request, identity: RequestIdentity) -> AliyunActor:
    principal = getattr(request.state, "auth_principal", None)
    session = getattr(request.state, "session_token", None)
    if (not isinstance(principal, AuthPrincipal) or not isinstance(session, SecretStr)
        or principal.tenant_id != identity.tenant_id or principal.employee_id != identity.employee.employee_id):
        raise HTTPException(401, "请先登录本人TradeOS账号")
    return AliyunActor(tenant_id=principal.tenant_id, employee_id=principal.employee_id, user_id=principal.user_id)


@router.get("/status", response_model=AliyunConnectionStatus)
async def status(request: Request, identity: Identity, response: Response) -> AliyunConnectionStatus:
    response.headers["Cache-Control"] = "no-store"
    service = getattr(request.app.state, "aliyun_web", None)
    if not isinstance(service, AliyunWebService):
        return AliyunConnectionStatus(configured=False)
    return await service.status(_actor(request, identity))


@router.post("/connect", response_model=AliyunConnectionStatus)
async def connect(body: AliyunConnectCommand, request: Request, identity: Identity, response: Response) -> AliyunConnectionStatus:
    response.headers["Cache-Control"] = "no-store"
    service = getattr(request.app.state, "aliyun_web", None)
    if not isinstance(service, AliyunWebService):
        raise HTTPException(503, "管理员尚未配置阿里邮箱连接")
    return await service.connect(_actor(request, identity), body)
