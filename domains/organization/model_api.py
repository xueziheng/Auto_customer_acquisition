"""平台模型连接配置；只向管理员提供非秘密状态。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from shared.schemas.model_connection import MODEL_ENDPOINTS


class ModelApiCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    expected_revision: int = Field(strict=True, ge=0)


class ModelApiUpdate(ModelApiCommand):
    provider: Literal["deepseek", "compatible"]
    base_url: str = Field(max_length=200)
    protocol: Literal["responses", "chat_completions"]
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")
    api_key: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def valid_connection(self) -> "ModelApiUpdate":
        if self.base_url not in MODEL_ENDPOINTS:
            raise ValueError("请选择支持的官方接口地址")
        if self.provider == "deepseek" and self.base_url not in MODEL_ENDPOINTS[:2]:
            raise ValueError("DeepSeek 只能使用官方接口")
        if self.api_key is not None:
            key = self.api_key.get_secret_value()
            if not 8 <= len(key) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError("API Key 格式无效")
        return self


class ModelApiView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: int = 0
    configuration_version: str | None = None
    provider: Literal["deepseek", "compatible"] = "deepseek"
    base_url: str = "https://api.deepseek.com"
    protocol: Literal["responses", "chat_completions"] = "responses"
    model: str = ""
    configured: bool = False
    updated_at: datetime | None = None
    test_status: Literal["not_run", "passed", "failed"] = "not_run"
    tested_at: datetime | None = None
    failure_code: str | None = None
    apply_status: Literal["draft", "pending", "applied", "failed"] = "draft"
    active_model: str | None = None
    active_provider: str | None = None
    active_version: str | None = None
    supported_endpoints: tuple[str, ...] = MODEL_ENDPOINTS
