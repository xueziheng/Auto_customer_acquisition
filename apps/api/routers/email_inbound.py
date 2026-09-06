"""入站受限HTTP技术入口；角色规则及当前身份重读由公开域端口执行。"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from apps.composition_support.email_inbound import InboundComposition
from shared.errors import PermissionDenied
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from workflows.reply_qualification.inbound_contracts import (
    InboundBindingRequest,
    InboundPageError,
    InboundRetryRequest,
    InboundReviewView,
    InboundStatus,
)

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter(prefix="/email-inbound")
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]
Dependencies = Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)]


def _composition(deps: ConfiguredApiDependencies) -> InboundComposition:
    if deps.email_inbound is None:
        raise HTTPException(503, "email_inbound_not_configured")
    return deps.email_inbound


async def _result[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except PermissionDenied:
        raise HTTPException(403, "permission_denied") from None
    except InboundPageError as error:
        status = (
            404
            if error.reason == "not_found"
            else 409
            if error.reason in {"binding_conflict", "cursor_conflict", "not_archived"}
            else 503
        )
        raise HTTPException(status, error.reason) from None
    except ToolGatewayError as error:
        raise HTTPException(
            403 if error.category is ToolErrorCategory.PERMISSION_DENIED else 503,
            "email_inbound_unavailable",
        ) from None
    except Exception:  # noqa: BLE001 - HTTP边界固定错误，不暴露底层详情
        raise HTTPException(503, "email_inbound_unavailable") from None


@router.get("/status", response_model=InboundStatus)
async def status(identity: Identity, dependencies: Dependencies) -> InboundStatus:
    return await _result(
        _composition(dependencies).management.status(
            identity.tenant_id, identity.employee.employee_id
        )
    )


@router.post("/binding", response_model=InboundStatus)
async def bind(
    body: InboundBindingRequest, identity: Identity, dependencies: Dependencies
) -> InboundStatus:
    return await _result(
        _composition(dependencies).management.bind(
            identity.tenant_id, identity.employee.employee_id, body.identity_id
        )
    )


@router.post("/retry", response_model=InboundStatus)
async def retry(
    body: InboundRetryRequest, identity: Identity, dependencies: Dependencies
) -> InboundStatus:
    return await _result(
        _composition(dependencies).management.retry(
            identity.tenant_id, identity.employee.employee_id, body.expected_version
        )
    )


@router.get("/reviews", response_model=list[InboundReviewView])
async def reviews(
    request: Request,
    identity: Identity,
    dependencies: Dependencies,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[
        str | None, Query(pattern=r"^irv_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    ] = None,
) -> list[InboundReviewView]:
    if set(request.query_params) - {"limit", "after"}:
        raise HTTPException(400, "invalid_parameters")
    return list(
        await _result(
            _composition(dependencies).management.reviews(
                identity.tenant_id,
                identity.employee.employee_id,
                limit=limit,
                after=after,
            )
        )
    )


@router.get(
    "/reviews/{review_id}/raw",
    response_class=Response,
    responses={
        200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
        **{status: {"model": ApiErrorResponse, "description": "固定入站错误"} for status in (400, 403, 404, 409, 503)},
    },
)
async def raw(
    review_id: str, request: Request, identity: Identity, dependencies: Dependencies
) -> Response:
    if request.query_params or await request.body():
        raise HTTPException(400, "invalid_parameters")
    c = _composition(dependencies)
    await _result(
        c.management.authorize_raw(
            identity.tenant_id, identity.employee.employee_id, review_id
        )
    )
    content = await _result(
        c.raw.read(identity.tenant_id, identity.employee.employee_id, review_id)
    )
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="inbound-review.eml"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
        },
    )
