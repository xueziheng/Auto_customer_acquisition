"""平台数据源 HTTP 边界：固定安全错误、限制体积、不回显凭证输入。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import ValidationError
from starlette.responses import Response

from apps.api.authentication import header_values
from apps.api.middleware import _error_response
from domains.organization.data_sources import (
    DataSourceAdministrationService,
    DataSourceConflict,
    DataSourceInputInvalid,
    DataSourceUnavailable,
)
from domains.organization.platform_access import PlatformAccessService
from domains.organization.schemas import (
    DataSourceBrowserCheck,
    DataSourceCredentialsDelete,
    DataSourceCredentialsUpdate,
    DataSourceNoteUpdate,
    DataSourceNoteView,
    DataSourcesView,
    DataSourceUsageCheck,
    DataSourceView,
)
from shared.authentication import AuthenticationDenied, AuthPrincipal


def create_data_sources_router(access: PlatformAccessService) -> APIRouter:
    """只挂载到独立平台应用，不为企业 API 增加凭证入口。"""
    router = APIRouter(tags=["platform-data-sources"])

    async def authorized(request: Request) -> tuple[AuthPrincipal, DataSourceAdministrationService]:
        principal = getattr(request.state, "auth_principal", None)
        if not isinstance(principal, AuthPrincipal):
            raise AuthenticationDenied()
        await access.authorize(principal)
        service = getattr(request.app.state, "data_sources", None)
        if not isinstance(service, DataSourceAdministrationService):
            raise DataSourceUnavailable("数据源配置不可用")
        return principal, service

    @router.get("/data-sources", response_model=DataSourcesView)
    async def list_sources(request: Request) -> DataSourcesView | Response:
        principal, service = await authorized(request)
        if request.query_params:
            return _error_response(400, code="data_source_input_invalid", message="数据源请求参数无效")
        return await service.list_sources(principal)

    @router.put(
        "/data-sources/{source_id}/credentials", response_model=DataSourceView,
        openapi_extra={"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": DataSourceCredentialsUpdate.model_json_schema()}},
        }},
    )
    async def save_credentials(source_id: str, request: Request) -> DataSourceView | Response:
        principal, service = await authorized(request)
        if request.query_params or header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(400, code="data_source_input_invalid", message="数据源请求格式无效")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 32768 - len(body):
                return _error_response(413, code="data_source_body_too_large", message="数据源请求过大")
            body.extend(chunk)
        try:
            command = DataSourceCredentialsUpdate.model_validate_json(bytes(body))
        except ValidationError:
            return _error_response(400, code="data_source_input_invalid", message="数据源配置输入无效")
        finally:
            body.clear()
        return await service.save_credentials(principal, source_id, command)

    @router.delete(
        "/data-sources/{source_id}/credentials", response_model=DataSourceView,
        openapi_extra={"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": DataSourceCredentialsDelete.model_json_schema()}},
        }},
    )
    async def delete_credentials(source_id: str, request: Request) -> DataSourceView | Response:
        principal, service = await authorized(request)
        if request.query_params or header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(400, code="data_source_input_invalid", message="数据源请求格式无效")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 1024 - len(body):
                return _error_response(413, code="data_source_body_too_large", message="数据源请求过大")
            body.extend(chunk)
        try:
            command = DataSourceCredentialsDelete.model_validate_json(bytes(body))
        except ValidationError:
            return _error_response(400, code="data_source_input_invalid", message="数据源配置输入无效")
        finally:
            body.clear()
        return await service.delete_credentials(principal, source_id, command)

    @router.post(
        "/data-sources/{source_id}/usage-check", response_model=DataSourceView,
        openapi_extra={"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": DataSourceUsageCheck.model_json_schema()}},
        }},
    )
    async def check_usage(source_id: str, request: Request) -> DataSourceView | Response:
        principal, service = await authorized(request)
        if request.query_params or header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(400, code="data_source_input_invalid", message="额度检查请求格式无效")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 1024 - len(body):
                return _error_response(413, code="data_source_body_too_large", message="请求过大")
            body.extend(chunk)
        try:
            command = DataSourceUsageCheck.model_validate_json(bytes(body))
        except ValidationError:
            return _error_response(400, code="data_source_input_invalid", message="额度检查输入无效")
        finally:
            body.clear()
        return await service.check_usage(principal, source_id, command)

    @router.post(
        "/data-sources/{source_id}/browser-check", response_model=DataSourceView,
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": DataSourceBrowserCheck.model_json_schema()}}}},
    )
    async def check_browser(source_id: str, request: Request) -> DataSourceView | Response:
        principal, service = await authorized(request)
        if request.query_params or header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(400, code="data_source_input_invalid", message="浏览器测试请求格式无效")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 1024 - len(body):
                return _error_response(413, code="data_source_body_too_large", message="请求过大")
            body.extend(chunk)
        try:
            DataSourceBrowserCheck.model_validate_json(bytes(body))
        except ValidationError:
            return _error_response(400, code="data_source_input_invalid", message="浏览器测试请求无效")
        finally:
            body.clear()
        return await service.check_browser(principal, source_id)

    @router.put(
        "/data-sources/{source_id}/note", response_model=DataSourceNoteView,
        openapi_extra={"requestBody": {
            "required": True,
            "content": {"application/json": {"schema": DataSourceNoteUpdate.model_json_schema()}},
        }},
    )
    async def save_note(source_id: str, request: Request) -> DataSourceNoteView | Response:
        principal, service = await authorized(request)
        if request.query_params or header_values(request.scope, b"content-type") != ["application/json"]:
            return _error_response(400, code="data_source_input_invalid", message="来源备注请求格式无效")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 16384 - len(body):
                return _error_response(413, code="data_source_body_too_large", message="来源备注请求过大")
            body.extend(chunk)
        try:
            command = DataSourceNoteUpdate.model_validate_json(bytes(body))
        except ValidationError:
            return _error_response(400, code="data_source_input_invalid", message="来源备注输入无效")
        finally:
            body.clear()
        return await service.save_note(principal, source_id, command)

    return router


async def data_source_error(request: Request, error: Exception) -> Response:
    """固定错误投影不包含输入、存储路径或供应商错误内容。"""
    del request
    if isinstance(error, DataSourceConflict):
        return _error_response(409, code="data_source_conflict", message="配置已被更新，请刷新后重新填写")
    if isinstance(error, DataSourceInputInvalid):
        return _error_response(400, code="data_source_input_invalid", message="数据源配置输入无效")
    return _error_response(503, code="data_source_unavailable", message="数据源配置暂不可用")
