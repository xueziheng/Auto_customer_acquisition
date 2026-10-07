"""API 进程存活与数据库就绪探针。"""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared.schemas.runtime_capabilities import RuntimeCapability


class ReadinessProbe(Protocol):
    """runtime 提供的最窄就绪检查能力。"""

    async def is_ready(self) -> bool: ...


def build_health_router(probe: ReadinessProbe) -> APIRouter:
    """构造固定响应的 health router；不暴露底层失败细节。"""
    router = APIRouter()

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @router.get("/health/ready", response_model=None)
    async def ready() -> dict[str, str] | JSONResponse:
        if await probe.is_ready():
            return {"status": "ready"}
        return JSONResponse(
            status_code=503,
            content={"code": "service_unavailable", "message": "服务暂时不可用"},
        )

    return router


def build_capability_router(capabilities: tuple[RuntimeCapability, ...]) -> APIRouter:
    """仅返回本进程安全组合状态，不暴露配置或代替 worker 健康。"""
    router = APIRouter()

    @router.get("/health/capabilities", response_model=list[RuntimeCapability])
    async def capabilities_view() -> tuple[RuntimeCapability, ...]:
        return capabilities

    return router
