"""阿里邮箱连接私有存储；API只返回无凭证的状态投影。"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domains.conversations.aliyun_connection import AliyunActor
from infra.pilot.config import checked_directory, private_read, private_write
from shared.errors import PermissionDenied


class AliyunBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    actor: AliyunActor
    email: str
    password: SecretStr = Field(repr=False)
    connected_at: datetime
    operation_id: str


class AliyunMailStore:
    """每个身份独立文件；调用方持数据库身份锁后才能发布。"""

    def __init__(self, root: Path):
        checked_directory(root)
        self.root = root

    def _path(self, actor: AliyunActor) -> Path:
        key = hashlib.sha256(f"{actor.tenant_id}\0{actor.employee_id}\0{actor.user_id}".encode()).hexdigest()
        return self.root / (key + ".json")

    def binding(self, actor: AliyunActor) -> AliyunBinding | None:
        path = self._path(actor)
        if not path.exists():
            return None
        try:
            result = AliyunBinding.model_validate_json(private_read(path))
            if result.actor != actor:
                raise ValueError()
            return result
        except Exception:  # noqa: BLE001 凭证解析错误只映射固定消息，绝不回显输入
            raise PermissionDenied("邮箱连接状态不可用") from None

    def publish(self, binding: AliyunBinding) -> None:
        value = binding.model_dump(mode="json")
        value["password"] = binding.password.get_secret_value()
        private_write(self._path(binding.actor), json.dumps(value).encode())
