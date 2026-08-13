"""RFC 8058 one-click unsubscribe 的匿名 capability 路由。"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from shared.errors import TransientError
from workflows.email_feedback.unsubscribe import UnsubscribeService

from ..middleware import ApiErrorResponse, ApiSettings

router = APIRouter(include_in_schema=False)

_PATH_RE = re.compile(r"/unsubscribe/[^/]+")
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_FORM_BODY = b"List-Unsubscribe=One-Click"
_MAX_BODY_BYTES = 256
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
}
_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>确认退订</title></head><body><main><h1>确认退订</h1><p>提交后将停止后续触达邮件。</p><form method="post"><input type="hidden" name="List-Unsubscribe" value="One-Click"><button type="submit">确认退订</button></form></main></body></html>"""


def is_anonymous_unsubscribe_route(method: str, path: str) -> bool:
    """只有 GET/POST + 单个 opaque token segment 可绕过租户断言。"""
    return method in {"GET", "POST"} and _PATH_RE.fullmatch(path) is not None


def _service(request: Request) -> UnsubscribeService | None:
    candidate = getattr(request.app.state, "unsubscribe_service", None)
    return candidate if isinstance(candidate, UnsubscribeService) else None


async def _read_bounded(request: Request) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        if not _append_bounded(body, chunk):
            return None
    return bytes(body)


def _append_bounded(body: bytearray, chunk: bytes) -> bool:
    remaining = _MAX_BODY_BYTES - len(body)
    if len(chunk) > remaining:
        return False
    body.extend(chunk)
    return True


def _has_exact_content_type(request: Request) -> bool:
    values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"content-type"
    ]
    return values == [_FORM_CONTENT_TYPE.encode("ascii")]


@router.get("/unsubscribe/{token}")
async def unsubscribe_confirmation(token: str) -> HTMLResponse:
    """只返回固定确认页；GET 永不查 token 或修改数据。"""
    del token
    return HTMLResponse(_PAGE, headers=_HEADERS)


def _unavailable(request: Request) -> Response:
    settings = getattr(request.app.state, "settings", None)
    retry_after = (
        settings.retry_after_seconds if isinstance(settings, ApiSettings) else 30
    )
    payload = ApiErrorResponse(
        code="service_unavailable", message="退订服务暂不可用"
    )
    return Response(
        status_code=503,
        content=payload.model_dump_json(),
        media_type="application/json",
        headers={**_HEADERS, "Retry-After": str(retry_after)},
    )


@router.post("/unsubscribe/{token}", status_code=204)
async def consume_unsubscribe(token: str, request: Request) -> Response:
    """外部统一 204；仅内部不可用明确 503，且不回显 capability。"""
    if not _has_exact_content_type(request):
        return Response(status_code=204, headers=_HEADERS)
    body = await _read_bounded(request)
    if body != _FORM_BODY:
        return Response(status_code=204, headers=_HEADERS)
    service = _service(request)
    if service is None:
        return Response(status_code=204, headers=_HEADERS)
    try:
        await service.consume(token)
    except TransientError:
        return _unavailable(request)
    except Exception:  # noqa: BLE001 capability 边界不得泄露内部异常
        return _unavailable(request)
    return Response(status_code=204, headers=_HEADERS)
