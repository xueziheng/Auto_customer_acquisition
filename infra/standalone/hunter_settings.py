"""Hunter 客户验证的显式非秘密元数据；构造不访问凭证文件。"""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from infra.pilot.config import private_read


class HunterCustomerSettings(BaseModel):
    """既有已验证配置版本与单租户每日上限，默认关闭。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    enabled: bool = Field(default=False, strict=True)
    business_tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$")
    configuration_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
    api_key_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
    credential_source: Literal["private_file", "platform"] = "private_file"
    secret_file: Path | None = Field(default=None, repr=False, exclude=True)
    source_directory: Path | None = Field(default=None, repr=False, exclude=True)
    control_tenant_id: str | None = Field(
        default=None, pattern=r"^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
    )
    source_revision: int | None = Field(default=None, strict=True, ge=1)
    maximum_daily_verifications: int = Field(strict=True, ge=1)

    @field_validator("secret_file", "source_directory")
    @classmethod
    def absolute_private_path(cls, value: Path | None) -> Path | None:
        if value is not None and (
            not value.is_absolute() or ".." in value.parts or value == Path("/")
        ):
            raise ValueError("Hunter 私有文件路径无效")
        return value

    @model_validator(mode="after")
    def exclusive_credential_source(self) -> Self:
        platform_fields = (
            self.source_directory, self.control_tenant_id, self.source_revision
        )
        if self.credential_source == "private_file":
            if self.secret_file is None or any(v is not None for v in platform_fields):
                raise ValueError("Hunter 凭证来源配置无效")
        elif (
            self.secret_file is not None
            or any(v is None for v in platform_fields)
            or self.configuration_version != f"hunter-platform-rev{self.source_revision}"
            or self.api_key_version != f"platform-rev{self.source_revision}"
        ):
            raise ValueError("Hunter 平台凭证版本绑定无效")
        return self


def load_hunter_customer_settings(path: Path | None) -> HunterCustomerSettings | None:
    """只读取显式配置文件；未传路径不启用，不搜索环境或读取引用密钥。"""
    if path is None:
        return None
    try:
        data = private_read(path)
        if len(data) > 65536:
            raise ValueError()
        return HunterCustomerSettings.model_validate_json(data)
    except Exception:  # noqa: BLE001 - 配置边界只输出固定安全错误
        raise ValueError("Hunter 客户验证配置无效") from None
