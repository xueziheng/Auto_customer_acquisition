"""只接受本owner生成的私有配置，绝不读取部署环境。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ControlledError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ControlledIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    employee_id: str
    user_id: str
    role: Literal["boss", "manager", "sales", "sourcing", "product"]
    label: str


class ControlledConfig(BaseModel):
    """配置不允许额外字段；敏感字段永不参与显示。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    owner: str = Field(pattern=r"^[a-f0-9]{32}$")
    tenant_id: str
    api_port: int = Field(gt=0, lt=65536)
    web_port: int = Field(gt=0, lt=65536)
    scheduler_port: int = Field(gt=0, lt=65536)
    notification_port: int = Field(gt=0, lt=65536)
    database_port: int = Field(gt=0, lt=65536)
    object_port: int = Field(gt=0, lt=65536)
    database_url: SecretStr
    bucket: str
    secrets: dict[str, SecretStr] = Field(repr=False)
    identities: tuple[ControlledIdentity, ...]

    def write(self, path: Path) -> None:
        payload = self.model_dump(mode="json", exclude={"secrets", "database_url"})
        payload["database_url"] = self.database_url.get_secret_value()
        payload["secrets"] = {k: v.get_secret_value() for k, v in self.secrets.items()}
        with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            json.dump(payload, stream)

    @classmethod
    def read(cls, path: Path) -> ControlledConfig:
        try:
            for item, mode in ((path, 0o600), (path.parent, 0o700)):
                stat = item.lstat()
                if (
                    item.is_symlink()
                    or stat.st_uid != os.getuid()
                    or stat.st_mode & 0o777 != mode
                ):
                    raise ValueError()
            result = cls.model_validate_json(path.read_bytes())
            if path.parent.name != "tradeos-controlled-" + result.owner:
                raise ValueError()
            from urllib.parse import urlsplit

            url = urlsplit(result.database_url.get_secret_value())
            if (
                url.scheme != "postgresql+asyncpg"
                or url.hostname != "127.0.0.1"
                or url.port != result.database_port
                or url.path != "/controlled"
            ):
                raise ValueError()
            return result
        except Exception:  # noqa: BLE001 安全边界只保留固定失败类别
            raise ControlledError("configuration_invalid") from None

    def resolve(self, reference: str) -> str:
        try:
            return self.secrets[reference].get_secret_value()
        except KeyError:
            raise ControlledError("secret_reference_rejected") from None

    def runtime_environment(self) -> dict[str, str]:
        """显式合成场景的技术配置；不会写入批准事实或活动业务政策。"""
        result = {k: v.get_secret_value() for k, v in self.secrets.items()}
        result.update(
            {
                "DATABASE_URL": self.database_url.get_secret_value(),
                "TRADEOS_TENANT_ID": self.tenant_id,
                "TRADEOS_DEV_MODE": "true",
                "TRADEOS_CORS_ALLOWED_ORIGINS": json.dumps(
                    [f"http://127.0.0.1:{self.web_port}"]
                ),
                "TRADEOS_API_RETRY_AFTER_SECONDS": "2",
                "TRADEOS_HANDOFF_POLICY": json.dumps(
                    {
                        "sla_seconds": 3600,
                        "backlog_threshold": 10,
                        "t1_seconds": 60,
                        "t2_seconds": 120,
                    }
                ),
                "TRADEOS_SCORING_POLICY": json.dumps(
                    {
                        "version": "controlled-v1",
                        "currency": "USD",
                        "value_band_boundaries": ["1000.00"],
                        "bucket_map": {str(k): "low" for k in range(1, 8)},
                    }
                ),
                "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
                "GMAIL_OAUTH_TOKEN_REF": "CONTROLLED_GMAIL",
                "TOOL_CALL_FINGERPRINT_KEY_REF": "CONTROLLED_FINGERPRINT",
                "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
                "TRADEOS_UNSUBSCRIBE_BASE_URL": f"http://127.0.0.1:{self.api_port}",
                "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "controlled",
                "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "v1",
                "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": json.dumps(
                    {"v1": "CONTROLLED_UNSUBSCRIBE"}
                ),
                "TRADEOS_TOOL_LEASE_SECONDS": "120",
                "S3_ENDPOINT": f"http://127.0.0.1:{self.object_port}",
                "S3_BUCKET_ARTIFACTS": self.bucket,
                "S3_ACCESS_KEY_REF": "CONTROLLED_OBJECT_ACCESS",
                "S3_SECRET_KEY_REF": "CONTROLLED_OBJECT_SECRET",
                "S3_REGION": "us-east-1",
                "RAW_ARTIFACT_MAX_BYTES": "10485760",
                "GENERATED_ARTIFACT_MAX_BYTES": "1048576",
                "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "1",
                "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
                "TRADEOS_SCHEDULER_LOCK_KEY": "74401",
                "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "3",
                "TRADEOS_HANDOFF_T1_SECONDS": "60",
                "TRADEOS_HANDOFF_T2_SECONDS": "120",
                "TRADEOS_DKIM_SELECTOR": "controlled",
                "TRADEOS_SCHEDULER_HEALTH_PORT": str(self.scheduler_port),
                "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
                "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
                "TRADEOS_TRADE_MANAGER_MODEL": "controlled-json-v1",
            }
        )
        return result
