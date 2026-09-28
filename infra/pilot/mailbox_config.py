"""本人邮箱的最小私有配置；不依赖业务政策，不读取 OAuth 内容。"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import URL

from infra.pilot.config import (
    PilotError,
    StrictModel,
    allocated_port,
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.identifiers import new_id


class MailboxBinding(StrictModel):
    """同一员工可拥有多个邮箱；凭证只保存显式绝对路径。"""

    binding_id: str = Field(pattern=r"^mbb_[a-f0-9]{32}$")
    employee_id: str = Field(pattern=r"^emp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    email: str = Field(min_length=3, max_length=254, repr=False)
    credentials_file: Path = Field(repr=False)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if (
            value != value.strip()
            or value.count("@") != 1
            or any(character.isspace() or ord(character) < 33 for character in value)
            or not all(value.split("@"))
        ):
            raise ValueError("binding_invalid")
        return value.casefold()

    @field_validator("credentials_file")
    @classmethod
    def absolute_credentials(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("binding_invalid")
        return value


class MailboxStorageIdentity(StrictModel):
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    volume_name: str = Field(pattern=r"^tradeos-mailbox-[a-f0-9]{32}-database$")
    volume_created_at: str = ""
    container_id: str = Field(default="", pattern=r"^([a-f0-9]{64})?$")


class MailboxConfig(StrictModel):
    """只含持久数据库、登录入口与明确授权的邮箱绑定。"""

    version: Literal[1] = 1
    owner_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    tenant_id: str = Field(pattern=r"^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    api_port: int = Field(strict=True, ge=1024, le=65535)
    db_port: int = Field(strict=True, ge=1024, le=65535)
    db_password: SecretStr = Field(repr=False)
    fingerprint_key: SecretStr = Field(repr=False)
    bindings: tuple[MailboxBinding, ...] = ()
    storage: MailboxStorageIdentity | None = None

    @model_validator(mode="after")
    def safe_bindings(self) -> MailboxConfig:
        if (
            self.api_port == self.db_port
            or len(self.db_password.get_secret_value()) < 32
            or len(self.fingerprint_key.get_secret_value()) < 32
            or len({binding.email for binding in self.bindings}) != len(self.bindings)
            or len({binding.binding_id for binding in self.bindings})
            != len(self.bindings)
            or (
                self.storage is not None
                and self.storage.volume_name
                != f"tradeos-mailbox-{self.owner_id}-database"
            )
        ):
            raise ValueError("configuration_invalid")
        return self

    @property
    def database_url(self) -> SecretStr:
        return SecretStr(
            URL.create(
                "postgresql+asyncpg",
                "mailbox",
                self.db_password.get_secret_value(),
                "127.0.0.1",
                self.db_port,
                "mailbox",
            ).render_as_string(False)
        )

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @classmethod
    def create(cls, path: Path) -> MailboxConfig:
        """显式创建新配置；不创建容器、账号或任何业务数据。"""
        path = path.absolute()
        if path.exists() or path.is_symlink():
            raise PilotError("profile_exists")
        for parent in path.parents:
            if parent.is_symlink():
                raise PilotError("configuration_invalid")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        checked_directory(path.parent)
        ports: set[int] = set()
        while len(ports) < 2:
            ports.add(allocated_port())
        api_port, db_port = sorted(ports)
        result = cls(
            owner_id=secrets.token_hex(16),
            tenant_id=str(new_id("tn")),
            api_port=api_port,
            db_port=db_port,
            db_password=SecretStr(secrets.token_hex(32)),
            fingerprint_key=SecretStr(secrets.token_hex(32)),
        )
        with exclusive_profile_lock(path.parent):
            if path.exists() or path.is_symlink():
                raise PilotError("profile_exists")
            result.write(path)
        return result

    @classmethod
    def read(cls, path: Path) -> MailboxConfig:
        try:
            return cls.model_validate_json(private_read(path))
        except Exception:  # noqa: BLE001 固定错误不带私有输入
            raise PilotError("configuration_invalid") from None

    def write(self, path: Path) -> None:
        """调用者须持操作锁；不可公开序列化技术凭证。"""
        payload = self.model_dump(
            mode="json", exclude={"db_password", "fingerprint_key"}
        )
        payload.update(
            db_password=self.db_password.get_secret_value(),
            fingerprint_key=self.fingerprint_key.get_secret_value(),
        )
        private_write(path, json.dumps(payload).encode())

    @classmethod
    def bind(
        cls, path: Path, *, employee_id: str, email: str, credentials_file: Path
    ) -> MailboxConfig:
        """安全追加同员工邮箱；禁止静默转移已有邮箱或替换授权文件。"""
        try:
            binding = MailboxBinding(
                binding_id="mbb_" + secrets.token_hex(16),
                employee_id=employee_id,
                email=email,
                credentials_file=credentials_file,
            )
        except Exception:  # noqa: BLE001 固定错误不回显邮箱或路径
            raise PilotError("binding_invalid") from None
        with exclusive_profile_lock(path.parent):
            config = cls.read(path)
            for current in config.bindings:
                if current.email == binding.email:
                    if (
                        current.employee_id != binding.employee_id
                        or current.credentials_file != binding.credentials_file
                    ):
                        raise PilotError("binding_conflict")
                    return config
            updated = config.model_copy(
                update={"bindings": (*config.bindings, binding)}
            )
            updated.write(path)
            return updated
