"""平台公司日交付额度：只访问组合根已绑定的公司服务。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import Response

from domains.organization.platform_access import PlatformAccessService
from domains.prospecting.service import InventoryService
from shared.authentication import AuthenticationDenied, AuthPrincipal
from shared.schemas.identifiers import TenantId

from ..authentication import header_values
from ..middleware import _error_response


class InventoryQuotaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    daily_limit: int = Field(strict=True, ge=0, le=10000)


class InventoryQuotaView(BaseModel):
    tenant_id: TenantId
    daily_limit: int
    used_today: int
    remaining_today: int
    reset_at: datetime


def create_inventory_quota_router(access: PlatformAccessService) -> APIRouter:
    """授权由控制租户确认；不能以路径参数构造新公司数据库连接。"""
    router = APIRouter(tags=["platform-inventory"])

    async def service_for(
        request: Request, tenant_id: str
    ) -> tuple[AuthPrincipal, InventoryService | None]:
        principal = getattr(request.state, "auth_principal", None)
        if not isinstance(principal, AuthPrincipal):
            raise AuthenticationDenied()
        await access.authorize(principal)
        services = cast(
            dict[str, InventoryService],
            getattr(request.app.state, "inventory_companies", {}),
        )
        return principal, services.get(tenant_id)

    async def quota(service: InventoryService, tenant_id: str) -> InventoryQuotaView:
        status = await service.status(TenantId(tenant_id))
        return InventoryQuotaView(
            tenant_id=status.tenant_id,
            daily_limit=status.daily_limit,
            used_today=status.used_today,
            remaining_today=status.remaining_today,
            reset_at=status.reset_at,
        )

    @router.get(
        "/inventory/companies/{tenant_id}/quota", response_model=InventoryQuotaView
    )
    async def read(tenant_id: str, request: Request) -> InventoryQuotaView | Response:
        _, service = await service_for(request, tenant_id)
        if service is None:
            return _error_response(
                404, code="inventory_unavailable", message="该企业候选服务未配置"
            )
        return await quota(service, tenant_id)

    @router.put(
        "/inventory/companies/{tenant_id}/quota",
        response_model=InventoryQuotaView,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": InventoryQuotaUpdate.model_json_schema()
                    }
                },
            }
        },
    )
    async def update(tenant_id: str, request: Request) -> InventoryQuotaView | Response:
        principal, service = await service_for(request, tenant_id)
        if service is None:
            return _error_response(
                404, code="inventory_unavailable", message="该企业候选服务未配置"
            )
        if request.query_params or header_values(request.scope, b"content-type") != [
            "application/json"
        ]:
            return _error_response(
                400, code="inventory_input_invalid", message="额度输入格式无效"
            )
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1024:
                return _error_response(
                    413, code="inventory_input_invalid", message="额度输入过大"
                )
            body.extend(chunk)
        try:
            command = InventoryQuotaUpdate.model_validate_json(bytes(body))
        except ValidationError:
            return _error_response(
                400, code="inventory_input_invalid", message="日额度须为0至10000的整数"
            )
        await access.authorize(principal)
        await service.set_daily_limit(TenantId(tenant_id), command.daily_limit)
        return await quota(service, tenant_id)

    return router
