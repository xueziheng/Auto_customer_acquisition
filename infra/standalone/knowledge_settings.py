"""企业资料处理的显式部署配置；不包含模型凭证或客户端路径。"""
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra.pilot.config import PilotError, private_read


class KnowledgeSettings(BaseModel):
    """独立根目录包含任务临时区与按企业划分的持久 vault。"""
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    enabled: bool = Field(strict=True)
    root: Path
    codex_binary: Path
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, strict=True, ge=1, le=10 * 1024 * 1024)
    max_text_chars: int = Field(default=60000, strict=True, ge=100, le=60000)
    timeout_seconds: int = Field(default=180, strict=True, ge=30, le=600)

    @field_validator("root", "codex_binary")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or value == Path("/"):
            raise ValueError("企业资料配置路径无效")
        return value

def load_knowledge_settings(path: Path) -> KnowledgeSettings:
    """只读取明确指定的私有文件，不扫描环境、不解析凭证。"""
    try:
        data = private_read(path)
        if len(data) > 65536:
            raise ValueError()
        return KnowledgeSettings.model_validate_json(data)
    except (PilotError, ValueError, OSError):
        raise ValueError("企业资料配置文件无效") from None
