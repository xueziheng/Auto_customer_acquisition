"""只接收受认证平台管理员命令，密钥验证错误固定脱敏。"""
from fastapi import APIRouter, Request
from pydantic import ValidationError

from apps.api.authentication import header_values
from apps.api.middleware import _error_response
from domains.organization.data_sources import DataSourceUnavailable
from domains.organization.model_api import ModelApiCommand
from domains.organization.model_catalog import (
    ModelCatalogEntry,
    ModelCatalogSave,
    ModelCatalogView,
    ModelCompanyUpdate,
)
from shared.authentication import AuthPrincipal
from shared.errors import PermissionDenied


def create_model_catalog_router() -> APIRouter:
    router = APIRouter()

    async def bound(request):
        principal = getattr(request.state, "auth_principal", None)
        if not isinstance(principal, AuthPrincipal):
            raise PermissionDenied("平台权限不足")
        service = getattr(request.app.state, "model_catalog", None)
        if service is None:
            raise DataSourceUnavailable("模型库尚未启用")
        await service.access.authorize(principal)
        return service, principal

    async def mutate(request, model, action):
        service, principal = await bound(request)
        if request.query_params or header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(400, code="model_input_invalid", message="请使用 JSON 配置")
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body)+len(chunk) > 16384:
                    return _error_response(413, code="model_input_large", message="模型配置过大")
                body.extend(chunk)
            try:
                command = model.model_validate_json(bytes(body))
            except ValidationError:
                return _error_response(400, code="model_input_invalid", message="模型配置无效，请检查名称、接口地址、模型名和密钥")
            return await action(service, principal, command)
        finally:
            body.clear()

    def schema(model):
        return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}

    @router.get("/model-catalog", response_model=ModelCatalogView)
    async def get(request: Request):
        service, principal = await bound(request)
        return await service.get(principal)

    @router.post("/model-catalog", response_model=ModelCatalogEntry, openapi_extra=schema(ModelCatalogSave))
    async def create(request: Request):
        return await mutate(request, ModelCatalogSave, lambda s,p,c: s.save(p, None, c))

    @router.put("/model-catalog/{entry_id}", response_model=ModelCatalogEntry, openapi_extra=schema(ModelCatalogSave))
    async def save(entry_id: str, request: Request):
        return await mutate(request, ModelCatalogSave, lambda s,p,c: s.save(p, entry_id, c))

    @router.post("/model-catalog/{entry_id}/delete", response_model=ModelCatalogView, openapi_extra=schema(ModelApiCommand))
    async def delete(entry_id: str, request: Request):
        return await mutate(request, ModelApiCommand, lambda s,p,c: s.delete(p, entry_id, c))

    @router.post("/model-catalog/{entry_id}/test", response_model=ModelCatalogEntry, openapi_extra=schema(ModelApiCommand))
    async def test(entry_id: str, request: Request):
        return await mutate(request, ModelApiCommand, lambda s,p,c: s.test(p, entry_id, c))

    @router.post("/model-catalog/{entry_id}/test-agent", response_model=ModelCatalogEntry, openapi_extra=schema(ModelApiCommand))
    async def test_agent(entry_id: str, request: Request):
        return await mutate(request, ModelApiCommand, lambda s,p,c: s.test(p, entry_id, c, agent=True))

    @router.put("/company-models/{company_id}", response_model=ModelCatalogView, openapi_extra=schema(ModelCompanyUpdate))
    async def bind(company_id: str, request: Request):
        return await mutate(request, ModelCompanyUpdate, lambda s,p,c: s.bind(p, company_id, c))

    return router
