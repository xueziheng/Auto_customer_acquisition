"""独立模型配置；无供应商回退、隐式额度或任意 URL。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from infra.pilot.config import PilotError, private_read
from shared.schemas.model_invocation import ModelLimits


class StandaloneResearchSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    secret_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=128, repr=False, exclude=True)
    exclusive_account_confirmed: Literal[True]
    playbook_reader_user_id: str = Field(min_length=1, max_length=128)
    maximum_artifact_bytes: int = Field(strict=True, gt=0)
    search_timeout_seconds: int = Field(strict=True, ge=1, le=30)
    page_timeout_seconds: int = Field(strict=True, ge=1, le=30)


class StandaloneModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    provider: Literal["deepseek"]
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")
    secret_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        repr=False,
        exclude=True,
    )
    configuration_version: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    limits: ModelLimits
    model_data_export_enabled: bool = Field(strict=True)
    research: StandaloneResearchSettings | None = None

    @model_validator(mode="after")
    def research_output_limit(self) -> "StandaloneModelSettings":
        if self.research is not None and not 128 <= self.limits.max_output_tokens <= 8192:
            raise ValueError("研究输出上限必须处于既有研究模型端口范围")
        return self


def load_model_settings(path: Path) -> StandaloneModelSettings:
    try:
        data = private_read(path)
        if len(data) > 65536:
            raise ValueError("配置文件过大")
        return StandaloneModelSettings.model_validate_json(data)
    except (PilotError, ValueError, OSError):
        raise ValueError("模型配置文件无效，请检查私有文件权限与必填字段") from None
