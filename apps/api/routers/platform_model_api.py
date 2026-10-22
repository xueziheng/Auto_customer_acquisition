"""平台模型 API 设置：受认证和 CSRF 保护，私有输入不进入 422 错误。"""
from fastapi import APIRouter, Request
from pydantic import ValidationError

from apps.api.authentication import header_values
from apps.api.middleware import _error_response
from domains.organization.data_sources import DataSourceUnavailable
from domains.organization.model_api import ModelApiCommand, ModelApiUpdate, ModelApiView
from shared.authentication import AuthPrincipal
from shared.errors import PermissionDenied


def create_model_api_router() -> APIRouter:
    router = APIRouter()

    def bound(request: Request):
        principal = getattr(request.state, "auth_principal", None)
        if not isinstance(principal, AuthPrincipal):
            raise PermissionDenied("平台权限不足")
        service = getattr(request.app.state, "model_api", None)
        if service is None:
            raise DataSourceUnavailable("模型配置尚未启用")
        return service, principal

    @router.get("/model-api", response_model=ModelApiView)
    async def get(request: Request):
        service, principal = bound(request)
        return await service.get(principal)

    async def mutate(request, model, action):
        service, principal = bound(request)
        await service.get(principal)
        if request.query_params or header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(400, code="model_input_invalid", message="请使用 JSON 配置")
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > 16384:
                    return _error_response(413, code="model_input_large", message="模型配置过大")
                body.extend(chunk)
            try:
                command = model.model_validate_json(bytes(body))
            except ValidationError:
                return _error_response(400, code="model_input_invalid", message="模型配置无效，请检查接口地址、模型名和密钥")
            return await getattr(service, action)(principal, command)
        finally:
            body.clear()

    def schema(model):
        return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}

    @router.put("/model-api", response_model=ModelApiView, openapi_extra=schema(ModelApiUpdate))
    async def save(request: Request):
        return await mutate(request, ModelApiUpdate, "save")

    @router.post("/model-api/test", response_model=ModelApiView, openapi_extra=schema(ModelApiCommand))
    async def test(request: Request):
        return await mutate(request, ModelApiCommand, "test")

    @router.post("/model-api/apply", response_model=ModelApiView, openapi_extra=schema(ModelApiCommand))
    async def apply(request: Request):
        return await mutate(request, ModelApiCommand, "apply")

    return router
