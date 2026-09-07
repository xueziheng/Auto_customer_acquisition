"""登录、当前会话与退出的私有 HTTP 适配；不暴露账号管理接口。"""

from typing import cast

from fastapi import APIRouter, FastAPI, Request
from pydantic import SecretStr, ValidationError
from starlette.responses import Response

from shared.authentication import (
    AuthenticationDenied,
    AuthenticationRateLimited,
    AuthenticationService,
    IssuedSession,
)

from ..authentication import (
    COOKIE_PATH,
    LoginRequest,
    SessionResponse,
    header_values,
)
from ..dependencies import get_api_dependencies, get_api_settings
from ..identity import resolve_request_identity
from ..middleware import _error_response

router = APIRouter(prefix="/auth", tags=["authentication"])


def _service(request: Request) -> AuthenticationService:
    """只读取显式装配的认证依赖；不回退到开发身份。"""
    service = getattr(request.app.state, "authentication", None)
    if service is None:
        raise AuthenticationDenied()
    return cast(AuthenticationService, service)


async def _private_response(request: Request, issued: IssuedSession) -> SessionResponse:
    """使用当前员工公共服务重建权限信息，验证映射后才向浏览器提供材料。"""
    request.state.auth_principal = issued.principal
    identity = await resolve_request_identity(
        request, get_api_settings(request), get_api_dependencies(request)
    )
    return SessionResponse(
        employee=identity.employee,
        csrf_token=issued.csrf_token.get_secret_value(),
        expires_at=issued.expires_at,
    )


@router.post(
    "/login",
    response_model=SessionResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/LoginRequest"}
                }
            },
        }
    },
)
async def login(request: Request, response: Response) -> SessionResponse | Response:
    """流式限制 4 KiB；所有校验错误固定脱敏，成功才轮换旧会话。"""
    response.headers["Cache-Control"] = "no-store"
    if header_values(request.scope, b"content-type") != ["application/json"]:
        return _error_response(
            415,
            code="authentication_json_required",
            message="登录需要JSON请求",
            headers={"Cache-Control": "no-store"},
        )
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > 4096 - len(body):
            return _error_response(
                413,
                code="authentication_body_too_large",
                message="登录请求过大",
                headers={"Cache-Control": "no-store"},
            )
        body.extend(chunk)
    try:
        credentials = LoginRequest.model_validate_json(bytes(body))
    except ValidationError:
        return _error_response(
            400,
            code="authentication_input_invalid",
            message="登录资料无效",
            headers={"Cache-Control": "no-store"},
        )
    service = _service(request)
    issued = await service.login(credentials.username, credentials.password)
    try:
        result = await _private_response(request, issued)
        old = getattr(request.state, "session_token", None)
        if isinstance(old, SecretStr):
            await service.logout(old)
    except Exception:
        await service.logout(issued.token)
        raise
    response.set_cookie(
        request.app.state.authentication_cookie_name,
        issued.token.get_secret_value(),
        httponly=True,
        samesite="strict",
        path=COOKIE_PATH,
        expires=issued.expires_at,
    )
    return result


@router.get("/session", response_model=SessionResponse)
async def current_session(request: Request, response: Response) -> SessionResponse:
    """只读恢复 CSRF 与当前员工，不刷新绝对到期时间。"""
    response.headers["Cache-Control"] = "no-store"
    token = getattr(request.state, "session_token", None)
    if not isinstance(token, SecretStr):
        raise AuthenticationDenied()
    issued = await _service(request).get_session(token)
    return await _private_response(request, issued)


@router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    """服务端撤销成功后清 cookie，失败不能伪装为已退出。"""
    token = getattr(request.state, "session_token", None)
    if not isinstance(token, SecretStr):
        raise AuthenticationDenied()
    await _service(request).logout(token)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        request.app.state.authentication_cookie_name,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
    )
    return response


def install_authentication_errors(app: FastAPI) -> None:
    """认证错误独立固定映射，不向日志传播输入材料。"""

    async def denied(request: Request, error: Exception) -> Response:
        del request, error
        return _error_response(
            401,
            code="authentication_required",
            message="账号或密码错误，或会话已失效",
            headers={"Cache-Control": "no-store"},
        )

    async def limited(request: Request, error: Exception) -> Response:
        del request, error
        return _error_response(
            429,
            code="authentication_rate_limited",
            message="尝试次数过多，请稍后重试",
            headers={"Cache-Control": "no-store"},
        )

    app.add_exception_handler(AuthenticationDenied, denied)
    app.add_exception_handler(AuthenticationRateLimited, limited)
