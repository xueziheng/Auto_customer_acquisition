"""显式政策与当前 owner 私有配置，不读取环境或外部凭证。"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import URL

from domains.opportunities.models import HandoffPolicy
from domains.opportunities.scoring import ScoringPolicy
from shared.schemas.identifiers import new_id
from shared.schemas.money import CurrencyCode, Money, WireDecimal

PILOT_GMAIL_MAILBOX_ALIAS = "pilot-gmail"


class PilotError(RuntimeError):
    """仅固定错误码可以越过进程边界。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class HandoffInput(StrictModel):
    sla_seconds: int = Field(strict=True, gt=0)
    backlog_threshold: int = Field(strict=True, gt=0)
    t1_seconds: int = Field(strict=True, gt=0)
    t2_seconds: int = Field(strict=True, gt=0)
    owner_reminder_interval_seconds: int | None = Field(
        default=None, strict=True, gt=0, le=2147483646
    )

    @model_validator(mode="after")
    def domain_validation(self) -> HandoffInput:
        HandoffPolicy(self.sla_seconds, self.backlog_threshold)
        return self


class ScoringInput(StrictModel):
    version: str = Field(min_length=1, max_length=128)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    value_band_boundaries: tuple[WireDecimal, ...]
    bucket_map: dict[
        Literal["1", "2", "3", "4", "5", "6", "7"], Literal["high", "mid", "low"]
    ]

    @model_validator(mode="after")
    def domain_validation(self) -> ScoringInput:
        if self.version != self.version.strip():
            raise ValueError("policy_invalid")
        ScoringPolicy(
            self.version,
            tuple(
                Money(v, CurrencyCode(self.currency))
                for v in self.value_band_boundaries
            ),
            {int(k): v for k, v in self.bucket_map.items()},
        )
        return self


class PilotPolicy(StrictModel):
    handoff_policy: HandoffInput
    scoring_policy: ScoringInput


class StorageIdentity(StrictModel):
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    volume_name: str = Field(pattern=r"^tradeos-pilot-[a-f0-9]{32}-(database|objects)$")
    volume_created_at: str = ""
    container_id: str = Field(default="", pattern=r"^([a-f0-9]{64})?$")


def checked_directory(path: Path) -> None:
    """拒绝路径中链接以及不属于当前用户的非私有 profile。"""
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PilotError("configuration_invalid")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PilotError("configuration_invalid")


def private_read(path: Path) -> bytes:
    """以 nofollow 打开并核验实际文件，避免路径检查与读取之间换成链接。"""
    try:
        checked_directory(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise PilotError("configuration_invalid") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise PilotError("configuration_invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def private_write(path: Path, payload: bytes) -> None:
    """私有同目录暂存、fsync、原子替换，不沿已有链接写入。调用者须持锁。"""
    checked_directory(path.parent)
    if path.exists() or path.is_symlink():
        private_read(path)
    pending = path.with_name(".pending-" + secrets.token_hex(16))
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        pending.unlink(missing_ok=True)


@contextmanager
def exclusive_profile_lock(path: Path) -> Iterator[None]:
    """操作级非阻塞锁；并发命令固定拒绝，不删除锁文件以免双 inode 锁。"""
    fd = -1
    try:
        checked_directory(path)
        fd = os.open(
            path / "profile.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise PilotError("configuration_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PilotError("profile_busy") from None
        yield
    finally:
        if fd != -1:
            os.close(fd)


def allocated_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class PilotGmailConfig(StrictModel):
    """本机内测唯一 Gmail 绑定；只保存地址、员工和私有凭证路径。"""

    address: str = Field(min_length=3, max_length=254, repr=False)
    credentials_file: Path = Field(repr=False)
    employee_id: str = Field(pattern=r"^emp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")

    @field_validator("address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        if (
            value != value.strip().casefold()
            or value.count("@") != 1
            or any(character.isspace() or ord(character) < 33 for character in value)
            or not all(value.split("@"))
        ):
            raise ValueError("configuration_invalid")
        return value

    @field_validator("credentials_file")
    @classmethod
    def absolute_credentials_file(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("configuration_invalid")
        return value


class PilotConfig(StrictModel):
    version: Literal[1] = 1
    restore_state: Literal["none", "pending", "failed", "complete"] = "none"
    owner: str = Field(pattern=r"^[a-f0-9]{32}$")
    tenant_id: str = Field(pattern=r"^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    api_port: int = Field(strict=True, gt=0, lt=65536)
    scheduler_port: int = Field(strict=True, gt=0, lt=65536)
    notification_port: int = Field(strict=True, gt=0, lt=65536)
    database_port: int = Field(strict=True, ge=0, lt=65536)
    object_port: int = Field(strict=True, ge=0, lt=65536)
    bucket: str = Field(pattern=r"^pilot-[a-f0-9]{32}$")
    secrets: dict[str, SecretStr] = Field(repr=False)
    policy: PilotPolicy
    storage: dict[Literal["database", "objects"], StorageIdentity]
    gmail: PilotGmailConfig | None = None

    @model_validator(mode="after")
    def validate_bindings(self) -> PilotConfig:
        if set(self.secrets) != {
            "PILOT_DATABASE_PASSWORD",
            "PILOT_OBJECT_ACCESS",
            "PILOT_OBJECT_SECRET",
            "PILOT_FINGERPRINT",
            "PILOT_UNSUBSCRIBE",
        }:
            raise ValueError("configuration_invalid")
        if any(len(s.get_secret_value()) < 32 for s in self.secrets.values()):
            raise ValueError("configuration_invalid")
        if len({self.api_port, self.scheduler_port, self.notification_port}) != 3:
            raise ValueError("configuration_invalid")
        if self.storage and set(self.storage) != {"database", "objects"}:
            raise ValueError("configuration_invalid")
        for kind, identity in self.storage.items():
            if identity.volume_name != f"tradeos-pilot-{self.owner}-{kind}":
                raise ValueError("configuration_invalid")
        return self

    @property
    def web_port(self) -> int:
        return self.api_port

    @property
    def database_url(self) -> SecretStr:
        return SecretStr(
            URL.create(
                "postgresql+asyncpg",
                "pilot",
                self.resolve("PILOT_DATABASE_PASSWORD"),
                "127.0.0.1",
                self.database_port,
                "pilot",
            ).render_as_string(False)
        )

    @classmethod
    def create(cls, path: Path, policy_file: Path) -> PilotConfig:
        """仅新私有目录，政策显式输入；不创建容器、不写任何业务数据。"""
        try:
            if policy_file.is_symlink() or not policy_file.is_file():
                raise ValueError()
            policy = PilotPolicy.model_validate_json(policy_file.read_bytes())
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("policy_invalid") from None
        if path.exists() or path.is_symlink():
            raise PilotError("profile_exists")
        for parent in path.parents:
            if parent.is_symlink():
                raise PilotError("configuration_invalid")
        path.mkdir(mode=0o700)
        owner = secrets.token_hex(16)
        ports: set[int] = set()
        while len(ports) < 3:
            ports.add(allocated_port())
        api, scheduler, notification = sorted(ports)
        config = cls(
            owner=owner,
            tenant_id=str(new_id("tn")),
            api_port=api,
            scheduler_port=scheduler,
            notification_port=notification,
            database_port=0,
            object_port=0,
            bucket="pilot-" + owner,
            secrets={
                key: SecretStr(secrets.token_hex(32))
                for key in (
                    "PILOT_DATABASE_PASSWORD",
                    "PILOT_OBJECT_ACCESS",
                    "PILOT_OBJECT_SECRET",
                    "PILOT_FINGERPRINT",
                    "PILOT_UNSUBSCRIBE",
                )
            },
            policy=policy,
            storage={},
        )
        with exclusive_profile_lock(path):
            config.write(path / "config.json")
        return config

    def write(self, path: Path) -> None:
        payload = self.model_dump(mode="json", exclude={"secrets"})
        payload["secrets"] = {
            key: value.get_secret_value() for key, value in self.secrets.items()
        }
        private_write(path, json.dumps(payload).encode())

    @classmethod
    def read(cls, path: Path) -> PilotConfig:
        try:
            return cls.model_validate_json(private_read(path))
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("configuration_invalid") from None

    def resolve(self, reference: str) -> str:
        try:
            return self.secrets[reference].get_secret_value()
        except KeyError:
            raise PilotError("secret_reference_rejected") from None

    def runtime_environment(self) -> dict[str, str]:
        """当前端口与显式业务政策；Gmail 只枚举固定引用，不枚举凭证内容。"""
        return {
            **(
                {"GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_REF"}
                if self.gmail is not None
                else {}
            ),
            **(
                {
                    "TRADEOS_HANDOFF_OWNER_REMINDER_INTERVAL_SECONDS": str(
                        self.policy.handoff_policy.owner_reminder_interval_seconds
                    )
                }
                if self.policy.handoff_policy.owner_reminder_interval_seconds
                is not None
                else {}
            ),
            "DATABASE_URL": self.database_url.get_secret_value(),
            "TRADEOS_TENANT_ID": self.tenant_id,
            "TRADEOS_DEV_MODE": "false",
            "TRADEOS_CORS_ALLOWED_ORIGINS": json.dumps(
                [f"http://127.0.0.1:{self.api_port}"]
            ),
            "TRADEOS_HANDOFF_POLICY": self.policy.handoff_policy.model_dump_json(),
            "TRADEOS_SCORING_POLICY": self.policy.scoring_policy.model_dump_json(),
            "TRADEOS_HANDOFF_T1_SECONDS": str(self.policy.handoff_policy.t1_seconds),
            "TRADEOS_HANDOFF_T2_SECONDS": str(self.policy.handoff_policy.t2_seconds),
            "TRADEOS_API_RETRY_AFTER_SECONDS": "2",
            "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
            "TOOL_CALL_FINGERPRINT_KEY_REF": "PILOT_FINGERPRINT",
            "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
            "TRADEOS_UNSUBSCRIBE_BASE_URL": f"http://127.0.0.1:{self.api_port}",
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "pilot",
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "v1",
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": json.dumps(
                {"v1": "PILOT_UNSUBSCRIBE"}
            ),
            "TRADEOS_TOOL_LEASE_SECONDS": "120",
            "S3_ENDPOINT": f"http://127.0.0.1:{self.object_port}",
            "S3_BUCKET_ARTIFACTS": self.bucket,
            "S3_ACCESS_KEY_REF": "PILOT_OBJECT_ACCESS",
            "S3_SECRET_KEY_REF": "PILOT_OBJECT_SECRET",
            "S3_REGION": "us-east-1",
            "RAW_ARTIFACT_MAX_BYTES": "10485760",
            "GENERATED_ARTIFACT_MAX_BYTES": "1048576",
            "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "1",
            "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
            "TRADEOS_SCHEDULER_LOCK_KEY": "74401",
            "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "3",
            "TRADEOS_SCHEDULER_HEALTH_PORT": str(self.scheduler_port),
            "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
            "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
            "TRADEOS_NOTIFICATION_POLL_INTERVAL_SECONDS": "1",
            "TRADEOS_NOTIFICATION_BATCH_LIMIT": "20",
            "TRADEOS_NOTIFICATION_HEALTH_PORT": str(self.notification_port),
            "TRADEOS_NOTIFICATION_LEASE_OWNER": "pilot-" + self.owner,
            "TRADEOS_NOTIFICATION_DELIVERY_MODE": "LOCAL_IN_APP",
            "PYTHON_DOTENV_DISABLED": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
