"""平台控制租户私有配置；只接受明确绑定的本机数据库身份。"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy.engine import URL

from infra.db.tenant_security import tenant_database_role
from infra.pilot.config import PilotError, private_read


class PlatformSettings(BaseModel):
    """控制租户独立凭证；不能由 HTTP 请求传入或改写。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    control_tenant_id: str = Field(pattern=r"^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    database_port: int = Field(strict=True, gt=0, lt=65536)
    database_username: str
    database_password: SecretStr = Field(repr=False, exclude=True)

    @model_validator(mode="after")
    def validate_identity(self) -> PlatformSettings:
        if (
            self.database_username != tenant_database_role(self.control_tenant_id)
            or not 32 <= len(self.database_password.get_secret_value()) <= 128
        ):
            raise ValueError("平台数据库身份无效")
        return self

    @property
    def database_url(self) -> SecretStr:
        """仅交给可信装配，序列化与 repr 均不公开运行凭证。"""
        return SecretStr(URL.create(
            "postgresql+asyncpg", self.database_username,
            self.database_password.get_secret_value(), "127.0.0.1",
            self.database_port, "pilot",
        ).render_as_string(False))


def load_platform_settings(path: Path) -> PlatformSettings:
    """读取指定的私有配置，不搜索环境或回退到企业凭证。"""
    try:
        data = private_read(path)
        if len(data) > 65536:
            raise ValueError()
        return PlatformSettings.model_validate_json(data)
    except (PilotError, ValueError, OSError):
        raise ValueError("平台配置文件无效") from None
