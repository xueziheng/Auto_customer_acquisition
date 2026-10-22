"""API与scheduler共用的Hunter惰性凭证解析；构造不读Key、不修改配置。"""
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from infra.data_source_credentials import FileDataSourceConfigurationRepository
from infra.pilot.config import private_read
from infra.standalone.hunter_settings import HunterCustomerSettings
from shared.schemas.identifiers import TenantId

HUNTER_CUSTOMER_KEY = "HUNTER_CUSTOMER_KEY"


class HunterCustomerSecretResolver:
    """仅 handler 可调用的固定逻辑引用；不缓存或展示密钥。"""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ValueError("Hunter 私有文件路径无效")
        self._path = path

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != HUNTER_CUSTOMER_KEY:
            raise ValueError("Hunter 凭证引用无效")
        try:
            data = private_read(self._path)
            if len(data) > 4096:
                raise ValueError()
            value = data.decode("ascii").strip()
            if not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
                raise ValueError()
            return value
        except Exception:  # noqa: BLE001 - 原文件/解码错误与内容不得穿透凭证边界
            raise ValueError("Hunter 凭证读取失败") from None


class HunterCustomerCredentialReader(Protocol):
    """受信 handler 只可读取部署绑定的数据源字段与精确修订。"""

    def resolve_credential(
        self, tenant_id: TenantId, source_id: str, field_name: str, *, revision: int
    ) -> SecretStr: ...


class PlatformHunterCustomerSecretResolver:
    """只在 handler 内解析平台明确授权的 Hunter 引用；轮换后失败关闭。"""

    def __init__(
        self,
        settings: HunterCustomerSettings,
        *,
        repository_factory: Callable[
            [Path, TenantId], HunterCustomerCredentialReader
        ] | None = None,
    ) -> None:
        if (
            settings.credential_source != "platform"
            or settings.source_directory is None
            or settings.control_tenant_id is None
            or settings.source_revision is None
        ):
            raise ValueError("Hunter 凭证来源配置无效")
        self._directory = settings.source_directory
        self._control_tenant = TenantId(settings.control_tenant_id)
        self._revision = settings.source_revision
        self._repository_factory = (
            repository_factory or FileDataSourceConfigurationRepository
        )

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != HUNTER_CUSTOMER_KEY:
            raise ValueError("Hunter 凭证引用无效")
        try:
            repository = self._repository_factory(self._directory, self._control_tenant)
            value = repository.resolve_credential(
                self._control_tenant, "hunter", "api_key", revision=self._revision
            ).get_secret_value()
            if (
                not value or len(value) > 4096
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            ):
                raise ValueError()
            return value
        except Exception:  # noqa: BLE001 - 平台仓库错误与内容不得穿透凭证边界
            raise ValueError("Hunter 凭证读取失败") from None
