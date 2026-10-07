"""非秘密模型配置；只有管理员显式探测才接纳一个付费任务。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from apps.api.dependencies import ConfiguredApiDependencies, get_api_dependencies
from apps.api.middleware import ApiErrorResponse
from apps.api.routers.assistant import Access, _safe
from apps.api.validation_route import ExplicitValidationRoute
from domains.assistant.schemas import (
    AssistantRegenerateInput,
    ModelSettingsUpdate,
    ModelSettingsView,
    TurnView,
)
from domains.assistant.service import ModelConfigurationService

router = APIRouter(
    prefix="/settings/model",
    tags=["model-settings"],
    route_class=ExplicitValidationRoute,
    responses={
        status: {"model": ApiErrorResponse} for status in (401, 403, 409, 422, 503)
    },
)


def _service(
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ModelConfigurationService:
    if dependencies.model_configuration is None:
        raise HTTPException(503)
    return dependencies.model_configuration


Configuration = Annotated[ModelConfigurationService, Depends(_service)]


@router.get("", response_model=ModelSettingsView)
async def get_model_settings(
    access: Access, service: Configuration
) -> ModelSettingsView:
    return await _safe(service.get_public(access[0]))


@router.post("", response_model=ModelSettingsView)
async def save_model_settings(
    body: ModelSettingsUpdate, access: Access, service: Configuration
) -> ModelSettingsView:
    return await _safe(service.save_nonsecret(access[0], body))


@router.post("/probe", response_model=TurnView, status_code=202)
async def probe_model(
    body: AssistantRegenerateInput, access: Access, service: Configuration
) -> TurnView:
    return await _safe(service.request_probe(access[0], body.idempotency_key))
