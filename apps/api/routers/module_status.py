"""尚未装配的 Phase 1 浅域统一、真实状态契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from ..dependencies import get_request_identity
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse


class Phase1ModuleStatus(BaseModel):
    """只描述可用性，不用空列表或假成功冒充已实现接口。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    module: str
    phase: Literal["phase1"] = "phase1"
    mode: Literal["manual", "audit", "configuration"]
    state: Literal["contract_only"] = "contract_only"
    reason_code: str


def build_status_router(
    *,
    module: str,
    mode: Literal["manual", "audit", "configuration"],
    reason_code: str,
) -> APIRouter:
    """构造需要已解析员工身份的只读状态端点。"""

    router = APIRouter()

    @router.get(
        "/status",
        response_model=Phase1ModuleStatus,
        operation_id=f"{module}_phase1_status",
        responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
    )
    async def get_status(
        _identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    ) -> Phase1ModuleStatus:
        return Phase1ModuleStatus(
            module=module,
            mode=mode,
            reason_code=reason_code,
        )

    return router
