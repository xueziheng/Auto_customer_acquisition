"""显式网页 Gmail 配置，凭证文件只交给连接器。"""
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from infra.pilot.config import private_read


class GmailWebSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    client_file: Path
    root: Path
    public_origin: str

    @field_validator("client_file", "root")
    @classmethod
    def absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or value == Path("/"):
            raise ValueError("Gmail 配置路径无效")
        return value

    @field_validator("public_origin")
    @classmethod
    def exact_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in {None, 443} or parsed.path or parsed.query or parsed.fragment
                or value != "https://" + parsed.netloc or parsed.netloc != parsed.netloc.lower()):
            raise ValueError("Gmail 回调必须使用固定 HTTPS 网站")
        return value

    @property
    def redirect_uri(self) -> str:
        return self.public_origin + "/inbox/mailbox/google-callback"


def load_gmail_settings(path: Path) -> GmailWebSettings:
    try:
        return GmailWebSettings.model_validate_json(private_read(path))
    except Exception:  # noqa: BLE001 - 私有授权边界只返回固定安全错误
        raise ValueError("Gmail 网页配置无效") from None
