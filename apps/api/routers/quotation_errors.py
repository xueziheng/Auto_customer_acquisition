"""仅新报价HTTP的固定错误映射；不改旧路由或全局400契约。"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from domains.costing.errors import (
    CostCoverageConflict,
    CostFreezeError,
    CostFreezeUnavailableError,
    CostingQuoteNotFoundError,
    CostSheetNotFoundError,
    InvalidPricingEvidenceError,
)
from domains.demand.errors import NeedUnitError, NeedUnitUnavailableError
from domains.quotations.errors import (
    QuotationError,
    QuotationUnavailableError,
    QuoteApprovalError,
    QuoteApprovalUnavailableError,
    QuoteContextError,
    QuoteContextUnavailableError,
    QuoteFileAccessError,
    QuoteFileAccessUnavailableError,
    QuoteFileError,
    QuoteFileUnavailableError,
)
from shared.errors import IdempotencyConflict, PermissionDenied, ValidationError
from shared.schemas.evidence_read import QuoteEvidenceError
from workflows.quote_approval.file_schemas import QuoteFileApplicationError


def _status(code: str) -> int:
    """只对已知类给出的封闭错误码分类，不匹配任意异常文字。"""
    if code in {
        "invalid_input",
        "unsupported_term",
        "template_unsupported",
        "source_unsupported",
        "parse_unsupported",
    }:
        return 400
    if code == "permission_denied":
        return 403
    if code in {
        "not_found",
        "record_not_found",
        "quote_not_found",
        "issuer_not_found",
        "need_not_found",
        "confirmation_not_found",
        "original_not_found",
        "metadata_not_found",
    }:
        return 404
    if code == "rate_limited":
        return 429
    if code in {
        "facts_corrupt",
        "dependency_unavailable",
        "lock_timeout",
        "storage_unknown",
        "storage_inconsistent",
        "source_unavailable",
        "parse_unavailable",
        "parse_timeout",
        "gateway_unavailable",
        "source_integrity_failed",
        "recovery_unavailable",
        "recovery_audit_unavailable",
        "recovery_audit_unknown",
        "invalid_config",
        "font_unavailable",
        "render_failed",
        "read_limit",
    }:
        return 503
    return 409


def _flat(status: int, code: str) -> JSONResponse:
    messages = {
        400: "报价请求无效",
        403: "当前员工无报价用途权限",
        404: "报价记录不存在",
        409: "报价当前条件不允许此操作",
        429: "报价文件调用过于频繁",
        503: "报价依赖不可用",
    }
    return JSONResponse(
        status_code=status, content={"code": code, "message": messages[status]}
    )


def quotation_error(error: Exception) -> JSONResponse:
    """真实文件调用保留专门技术字段；其他错误一律固定双字段。"""
    if isinstance(error, QuoteFileApplicationError):
        detail = error.detail
        status = _status(detail.code)
        if detail.tool_call_id is not None:
            if status == 400:
                status = 409
            headers = {}
            retry = detail.retry_after_seconds
            if type(retry) is int and 1 <= retry <= 86400:
                headers["Retry-After"] = str(retry)
            return JSONResponse(
                status_code=status,
                content=detail.model_dump(mode="json"),
                headers=headers,
            )
        return _flat(status, detail.code)
    if isinstance(error, PermissionDenied):
        return _flat(403, "permission_denied")
    if isinstance(error, CostingQuoteNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "record_not_found", "message": "成本报价记录不存在"},
        )
    if isinstance(error, CostSheetNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "record_not_found", "message": "成本表不存在"},
        )
    if isinstance(
        error,
        (
            CostFreezeUnavailableError,
            NeedUnitUnavailableError,
            QuotationUnavailableError,
            QuoteApprovalUnavailableError,
            QuoteContextUnavailableError,
            QuoteFileUnavailableError,
            QuoteFileAccessUnavailableError,
        ),
    ):
        return _flat(503, error.code)
    if isinstance(
        error,
        (
            CostFreezeError,
            NeedUnitError,
            QuotationError,
            QuoteApprovalError,
            QuoteContextError,
            QuoteFileError,
            QuoteFileAccessError,
            QuoteEvidenceError,
        ),
    ):
        return _flat(_status(error.code), error.code)
    if isinstance(error, IdempotencyConflict):
        return _flat(409, "idempotency_conflict")
    if isinstance(error, CostCoverageConflict):
        return _flat(409, "coverage_stale")
    if isinstance(error, InvalidPricingEvidenceError):
        return _flat(409, "evidence_invalid")
    if isinstance(error, ValidationError):
        return _flat(400, "invalid_input")
    return _flat(503, "dependency_unavailable")


class QuotationRoute(APIRoute):
    """在本路由内覆盖服务和依赖错误；取消不属于Exception且原样传播。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe(request: Request) -> Response:
            try:
                return await handler(request)
            except (RequestValidationError, HTTPException):
                raise
            except Exception as error:  # noqa: BLE001 -- 新HTTP明确固定503，不泄异常原文
                return quotation_error(error)

        return safe
