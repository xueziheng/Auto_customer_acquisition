"""独立模型配置；无供应商回退、隐式额度或任意 URL。"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from infra.pilot.config import PilotError, private_read
from infra.standalone.qq_continuous_settings import QQContinuousSettings
from infra.standalone.qq_trial_settings import QQTrialLaunchSettings
from shared.schemas.model_invocation import ModelLimits
from shared.schemas.search_backend import InventorySearchPolicy


class StandaloneResearchSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    secret_ref: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]*$", max_length=128, repr=False, exclude=True
    )
    exclusive_account_confirmed: Literal[True]
    playbook_reader_user_id: str = Field(min_length=1, max_length=128)
    maximum_artifact_bytes: int = Field(strict=True, gt=0)
    search_timeout_seconds: int = Field(strict=True, ge=1, le=30)
    page_timeout_seconds: int = Field(strict=True, ge=1, le=30)


class QQReplySettings(BaseModel):
    """受限私有测试回复：邮箱员工与模型受托老板分别显式绑定。"""
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    employee_id: str = Field(pattern=r"^emp_[A-Za-z0-9]+$", max_length=40)
    user_id: str = Field(min_length=1, max_length=128)
    classifier_employee_id: str = Field(pattern=r"^emp_[A-Za-z0-9]+$", max_length=40)
    mailbox_directory: Path = Field(repr=False, exclude=True)
    address: Literal["1249583590@qq.com"] = "1249583590@qq.com"
    recipient: Literal["986647123@qq.com"] = "986647123@qq.com"
    attempt_id: str | None = Field(default=None, pattern=r"^mat_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    trial: QQTrialLaunchSettings | None = None
    recovery_only: bool = Field(default=False, strict=True)
    continuous: QQContinuousSettings | None = None

    @model_validator(mode="after")
    def private_directory(self) -> "QQReplySettings":
        if not self.mailbox_directory.is_absolute() or ".." in self.mailbox_directory.parts:
            raise ValueError("QQ私有邮箱目录必须为明确绝对路径")
        if self.classifier_employee_id == self.employee_id:
            raise ValueError("测试邮箱负责人不能兼作受托分类老板")
        if self.recovery_only and self.trial is not None:
            raise ValueError("恢复已有回信不能启动新的QQ试验")
        if self.continuous is not None and (
            self.recovery_only or self.trial is not None or self.attempt_id is None
        ):
            raise ValueError("持续收信必须独立启用并绑定已发送尝试")
        if self.trial is not None and self.attempt_id is None:
            raise ValueError("单次启动必须绑定唯一发送尝试")
        if self.trial is not None and self.trial.delivery is not None:
            delivery = self.trial.delivery
            if (delivery.employee_id != self.classifier_employee_id
                or delivery.user_id != self.trial.classifier_user_id):
                raise ValueError("自有收件邮箱须由本次受托老板明确连接")
        return self


class StandaloneModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    provider: Literal["deepseek", "compatible"]
    api_base_url: str = "https://api.deepseek.com"
    api_protocol: Literal["responses", "chat_completions"] = "responses"
    api_key_file: Path | None = Field(default=None, repr=False, exclude=True)
    api_control_tenant: str | None = None
    api_connection_version: str | None = None
    model_catalog_directory: Path | None = Field(default=None, repr=False, exclude=True)
    codex_binary: Path = Path("/usr/local/bin/codex")
    codex_job_root: Path | None = Field(default=None, repr=False, exclude=True)
    api_verified_at: datetime | None = None
    api_business_tenant: str | None = None
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
    reply_enabled: bool = Field(default=False, strict=True)
    automatic_send_enabled: bool = Field(default=False, strict=True)
    qq_reply: QQReplySettings | None = None
    research: StandaloneResearchSettings | None = None
    inventory_search: InventorySearchPolicy | None = None

    @model_validator(mode="after")
    def research_output_limit(self) -> "StandaloneModelSettings":
        if (
            self.research is not None
            and self.limits.max_output_tokens < 128
        ):
            raise ValueError("研究输出上限不得低于128；研究端口另限8192")
        if self.reply_enabled and self.limits.max_output_tokens < 128:
            raise ValueError("回复输出上限不得低于128；回复端口另限8192")
        if self.qq_reply is not None and (not self.reply_enabled or self.automatic_send_enabled):
            raise ValueError("QQ私有测试只允许显式回信处理，自动发送必须关闭")
        from shared.schemas.model_connection import MODEL_ENDPOINTS
        if self.api_base_url not in MODEL_ENDPOINTS:
            raise ValueError("模型接口地址无效")
        if self.api_key_file is not None and (not self.api_key_file.is_absolute() or ".." in self.api_key_file.parts or not self.api_control_tenant):
            raise ValueError("模型密钥文件绑定无效")
        if self.model_catalog_directory is not None:
            if not self.api_control_tenant or not self.api_business_tenant or self.codex_job_root is None:
                raise ValueError("公司模型运行配置不完整")
            for path in (self.model_catalog_directory, self.codex_job_root, self.codex_binary):
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError("公司模型路径无效")
        return self


def load_model_settings(path: Path) -> StandaloneModelSettings:
    try:
        data = private_read(path)
        if len(data) > 65536:
            raise ValueError("配置文件过大")
        return StandaloneModelSettings.model_validate_json(data)
    except (PilotError, ValueError, OSError):
        raise ValueError("模型配置文件无效，请检查私有文件权限与必填字段") from None


def deployment_verification(settings: StandaloneModelSettings) -> tuple[str, str, datetime] | None:
    """已通过平台网关测试的精确企业/版本，不能授权其他配置。"""
    if settings.api_key_file is None or settings.api_verified_at is None or settings.api_business_tenant is None:
        return None
    return settings.api_business_tenant, settings.configuration_version, settings.api_verified_at
