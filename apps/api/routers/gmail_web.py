"""本人 Gmail 的网页授权和固定诊断；业务身份只能来自已验证会话。"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import SecretStr

from apps.api.composition.gmail_web import GmailWebService
from apps.api.dependencies import get_request_identity
from apps.api.identity import RequestIdentity
from domains.conversations.gmail_connection import (
    GmailActor,
    GmailAddress,
    GmailAuthorizationComplete,
    GmailAuthorizationStart,
    GmailConnectionStatus,
    GmailTestCommand,
    GmailTestResult,
    GmailTestTemplate,
)
from shared.authentication import AuthPrincipal

router = APIRouter(prefix="/inbox/gmail", tags=["private-gmail"])
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]


def _service(request: Request) -> GmailWebService:
    service = getattr(request.app.state, "gmail_web", None)
    if not isinstance(service, GmailWebService):
        raise HTTPException(503, "管理员尚未配置网页 Gmail 连接")
    return service


def _actor(request: Request, identity: RequestIdentity) -> tuple[GmailActor, SecretStr]:
    principal = getattr(request.state, "auth_principal", None)
    session = getattr(request.state, "session_token", None)
    if (not isinstance(principal, AuthPrincipal) or not isinstance(session, SecretStr)
            or principal.tenant_id != identity.tenant_id
            or principal.employee_id != identity.employee.employee_id):
        raise HTTPException(401, "请先登录 TradeOS 再连接 Gmail")
    return GmailActor(tenant_id=principal.tenant_id, employee_id=principal.employee_id,
                      user_id=principal.user_id), session


@router.get("/status", response_model=GmailConnectionStatus)
async def status(request: Request, identity: Identity, response: Response) -> GmailConnectionStatus:
    response.headers["Cache-Control"] = "no-store"
    if getattr(request.app.state, "gmail_web", None) is None:
        return GmailConnectionStatus(configured=False)
    actor, _ = _actor(request, identity)
    return await _service(request).status(actor)


@router.post("/start", response_model=GmailAuthorizationStart)
async def start(body: GmailAddress, request: Request, identity: Identity) -> GmailAuthorizationStart:
    actor, session = _actor(request, identity)
    return await _service(request).start(actor, session, body.email)


@router.post("/complete", response_model=GmailConnectionStatus)
async def complete(body: GmailAuthorizationComplete, request: Request,
                   identity: Identity) -> GmailConnectionStatus:
    actor, session = _actor(request, identity)
    return await _service(request).complete(actor, session, body)


@router.get("/test", response_model=GmailTestResult | None)
async def latest_test(request: Request, identity: Identity, response: Response) -> GmailTestResult | None:
    response.headers["Cache-Control"] = "no-store"
    actor, _ = _actor(request, identity)
    return await _service(request).latest_test(actor)


@router.post("/test", response_model=GmailTestResult)
async def send_test(body: GmailTestCommand, request: Request, identity: Identity) -> GmailTestResult:
    actor, _ = _actor(request, identity)
    return await _service(request).send_test(actor, body)


@router.get("/template", response_model=GmailTestTemplate)
async def template(request: Request, identity: Identity) -> GmailTestTemplate:
    actor, _ = _actor(request, identity)
    return await _service(request).template(actor)
