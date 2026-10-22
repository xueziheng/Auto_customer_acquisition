"""平台模型库与公司固定选择的非秘密契约。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domains.organization.model_api import ModelApiCommand, ModelApiUpdate, ModelApiView
from shared.schemas.model_connection import MODEL_ENDPOINTS


class ModelCatalogSave(ModelApiUpdate):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("请输入配置名称")
        return value.strip()


class ModelCatalogEntry(ModelApiView):
    id: str
    name: str
    agent_test_status: Literal["not_run", "passed", "failed"] = "not_run"
    agent_tested_at: datetime | None = None
    agent_failure_code: str | None = None
    used_by: tuple[str, ...] = ()


class ModelCompanyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str
    name: str
    enabled: bool
    runtime_available: bool
    revision: int = 0
    model_id: str | None = None
    configuration_version: str | None = None
    model: str | None = None
    configuration_name: str | None = None


class ModelCatalogView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    supported_endpoints: tuple[str, ...] = MODEL_ENDPOINTS
    models: tuple[ModelCatalogEntry, ...]
    companies: tuple[ModelCompanyBinding, ...]


class ModelCompanyUpdate(ModelApiCommand):
    model_id: str = Field(pattern=r"^(legacy|mc_[0-9A-HJKMNP-TV-Z]{26})$")
    model_revision: int = Field(strict=True, ge=1)
