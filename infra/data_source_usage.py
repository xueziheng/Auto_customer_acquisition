"""额度快照独立于凭证文件保存，绑定租户、来源、配置版本与真实工具调用。"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from domains.organization.data_sources import DataSourceConflict, DataSourceUnavailable
from infra.data_source_credentials import FileDataSourceConfigurationRepository
from infra.pilot.config import (
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.identifiers import TenantId
from shared.schemas.provider_usage import ProviderUsageObservation


class _UsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str
    source_id: str
    revision: int
    actor_id: str
    tool_call_id: str
    observation: ProviderUsageObservation


class FileDataSourceUsageRepository:
    """不改变凭证 envelope，旧版回滚仍可读取原凭证文件。"""

    def __init__(self, directory: Path, tenant: TenantId, credentials: FileDataSourceConfigurationRepository) -> None:
        self._directory, self._tenant, self._credentials = directory, tenant, credentials

    def _path(self, tenant: TenantId, source_id: str) -> Path:
        if tenant != self._tenant or source_id != "hunter":
            raise DataSourceUnavailable("额度记录不可用")
        return self._directory / "usage-hunter.json"

    def read(self, tenant: TenantId, source_id: str, revision: int) -> ProviderUsageObservation | None:
        """旧凭证对应快照不可在新版本显示；损坏记录不能伪装余额为零。"""
        path = self._path(tenant, source_id)
        try:
            if not path.exists() and not path.is_symlink():
                return None
            checked_directory(self._directory)
            record = _UsageRecord.model_validate_json(private_read(path))
            if record.tenant_id != tenant or record.source_id != source_id:
                raise ValueError("usage_binding_invalid")
            return record.observation if record.revision == revision else None
        except Exception:  # noqa: BLE001 -- 私有文件错误不能暴露路径或内容
            raise DataSourceUnavailable("额度记录不可用") from None

    def record(
        self, tenant: TenantId, source_id: str, revision: int, *, actor_id: str,
        tool_call_id: str, observation: ProviderUsageObservation,
    ) -> None:
        """完成 Gateway 后锁内核对当前配置；轮换/删除时在途旧结果不得生效。"""
        path = self._path(tenant, source_id)
        try:
            with exclusive_profile_lock(self._directory):
                current = self._credentials.list_configurations(tenant).get(source_id)
                if current is None or not current.configured or current.revision != revision:
                    raise DataSourceConflict("配置已更新")
                previous = self.read(tenant, source_id, revision)
                if previous is not None and previous.checked_at > observation.checked_at:
                    return
                private_write(path, _UsageRecord(
                    tenant_id=tenant, source_id=source_id, revision=revision,
                    actor_id=actor_id, tool_call_id=tool_call_id, observation=observation,
                ).model_dump_json().encode())
        except DataSourceConflict:
            raise
        except Exception:  # noqa: BLE001 -- 私有文件错误不能暴露路径或内容
            raise DataSourceUnavailable("额度记录不可用") from None
