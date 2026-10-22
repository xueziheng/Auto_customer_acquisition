"""平台私有数据源配置：原子版本、租户绑定和只写凭证存储。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domains.organization.data_sources import (
    DataSourceConflict,
    DataSourceInputInvalid,
    DataSourceUnavailable,
)
from domains.organization.schemas import (
    DataSourceConfigurationView,
    DataSourceCredentialsDelete,
    DataSourceCredentialsUpdate,
)
from infra.pilot.config import (
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.discovery_sources import get_discovery_source
from shared.schemas.identifiers import TenantId


class _Record(DataSourceConfigurationView):
    credentials: dict[str, SecretStr] = Field(repr=False)


class _Audit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    tenant_id: str
    actor_id: str
    source_id: str
    configuration_version: str
    revision: int
    action: Literal["credentials_updated", "credentials_deleted", "test_recorded"]
    occurred_at: datetime
    test_scope: Literal["account", "sample"] | None = None
    test_status: Literal["passed", "failed", "inconclusive"] | None = None


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    tenant_id: str
    schema_version: Literal[1] = 1
    records: dict[str, _Record] = Field(default_factory=dict, repr=False)
    audit: list[_Audit] = Field(default_factory=list)


class FileDataSourceConfigurationRepository:
    """只接收部署指定目录，不发现 .env，不复用或扩展原 profile secrets。"""

    def __init__(self, directory: Path, tenant_id: TenantId) -> None:
        if not directory.is_absolute() or ".." in directory.parts or not tenant_id:
            raise DataSourceUnavailable("数据源配置不可用")
        self._directory, self._tenant = directory, tenant_id
        self._file = directory / "configuration.json"

    def __repr__(self) -> str:
        return "FileDataSourceConfigurationRepository()"

    def _check_tenant(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant:
            raise DataSourceUnavailable("数据源配置不可用")

    def _read(self) -> _Envelope:
        checked_directory(self._directory)
        if not self._file.exists() and not self._file.is_symlink():
            return _Envelope(tenant_id=self._tenant)
        envelope = _Envelope.model_validate_json(private_read(self._file))
        if envelope.tenant_id != self._tenant or any(
            audit.tenant_id != self._tenant for audit in envelope.audit
        ):
            raise DataSourceUnavailable("数据源配置不可用")
        for source_id, record in envelope.records.items():
            definition = get_discovery_source(source_id)
            if (
                definition is None or not definition.credential_fields
                or set(record.credentials) != ({field.name for field in definition.credential_fields} if record.configured else set())
                or record.revision < 1 or not record.configuration_version
            ):
                raise DataSourceUnavailable("数据源配置不可用")
            if record.configured:
                DataSourceCredentialsUpdate(expected_revision=record.revision, credentials=record.credentials)
        return envelope

    def _write(self, envelope: _Envelope) -> None:
        payload = envelope.model_dump(mode="json")
        for source_id, record in envelope.records.items():
            payload["records"][source_id]["credentials"] = {
                name: value.get_secret_value() for name, value in record.credentials.items()
            }
        private_write(self._file, json.dumps(payload, ensure_ascii=False).encode())

    @staticmethod
    def _view(record: _Record) -> DataSourceConfigurationView:
        return DataSourceConfigurationView.model_validate(record.model_dump(exclude={"credentials"}))

    def list_configurations(self, tenant_id: TenantId) -> dict[str, DataSourceConfigurationView]:
        """原子文件读取不创建配置；权限或数据损坏固定失败关闭。"""
        self._check_tenant(tenant_id)
        try:
            if not self._directory.exists() and not self._directory.is_symlink():
                checked_directory(self._directory.parent)
                return {}
            return {source_id: self._view(record) for source_id, record in self._read().records.items()}
        except Exception:  # noqa: BLE001 -- 存储异常可能含私密输入，只返回固定分类
            raise DataSourceUnavailable("数据源配置不可用") from None

    def save_credentials(
        self, tenant_id: TenantId, source_id: str, actor_id: str,
        command: DataSourceCredentialsUpdate,
    ) -> DataSourceConfigurationView:
        """锁内校验旧版并一次保存新凭证、失效测试与安全审计。"""
        self._check_tenant(tenant_id)
        definition = get_discovery_source(source_id)
        if (
            definition is None or not definition.credential_fields
            or set(command.credentials) != {field.name for field in definition.credential_fields}
        ):
            raise DataSourceInputInvalid("数据源配置输入无效")
        try:
            checked_directory(self._directory.parent)
            self._directory.mkdir(mode=0o700, exist_ok=True)
            with exclusive_profile_lock(self._directory):
                envelope = self._read()
                previous = envelope.records.get(source_id)
                if command.expected_revision != (previous.revision if previous else 0):
                    raise DataSourceConflict("数据源配置已更新，请刷新")
                now = datetime.now(UTC)
                version = "dsc_" + uuid4().hex[:28]
                record = _Record(
                    configured=True, revision=command.expected_revision + 1,
                    configuration_version=version, updated_at=now, updated_by=actor_id,
                    credentials=command.credentials,
                )
                envelope.records[source_id] = record
                envelope.audit.append(_Audit(
                    tenant_id=self._tenant, actor_id=actor_id, source_id=source_id,
                    configuration_version=version, revision=record.revision,
                    action="credentials_updated", occurred_at=now,
                ))
                self._write(envelope)
                return self._view(record)
        except DataSourceConflict:
            raise
        except Exception:  # noqa: BLE001 -- 存储异常可能含私密输入，只返回固定分类
            raise DataSourceUnavailable("数据源配置不可用") from None

    def resolve_credential(
        self, tenant_id: TenantId, source_id: str, field_name: str, *, revision: int,
    ) -> SecretStr:
        """仅供受信 Gateway/Connector 组合读取精确版本；不注册为 Agent 工具。"""
        self._check_tenant(tenant_id)
        try:
            record = self._read().records[source_id]
            if record.revision != revision:
                raise DataSourceConflict("数据源配置已更新，请刷新")
            return record.credentials[field_name]
        except DataSourceConflict:
            raise
        except Exception:  # noqa: BLE001 -- 存储异常可能含私密输入，只返回固定分类
            raise DataSourceUnavailable("数据源配置不可用") from None

    def delete_credentials(
        self, tenant_id: TenantId, source_id: str, actor_id: str,
        command: DataSourceCredentialsDelete,
    ) -> DataSourceConfigurationView:
        """原子清除整组凭证；墓碑和审计阻止旧版写入或旧测试复活。"""
        self._check_tenant(tenant_id)
        definition = get_discovery_source(source_id)
        if definition is None or not definition.credential_fields:
            raise DataSourceInputInvalid("数据源配置输入无效")
        try:
            checked_directory(self._directory.parent)
            self._directory.mkdir(mode=0o700, exist_ok=True)
            with exclusive_profile_lock(self._directory):
                envelope = self._read()
                previous = envelope.records.get(source_id)
                if command.expected_revision != (previous.revision if previous else 0):
                    raise DataSourceConflict("数据源配置已更新，请刷新")
                now, version = datetime.now(UTC), "dsc_" + uuid4().hex[:28]
                record = _Record(
                    configured=False, revision=command.expected_revision + 1,
                    configuration_version=version, updated_at=now, updated_by=actor_id,
                    credentials={},
                )
                envelope.records[source_id] = record
                envelope.audit.append(_Audit(
                    tenant_id=self._tenant, actor_id=actor_id, source_id=source_id,
                    configuration_version=version, revision=record.revision,
                    action="credentials_deleted", occurred_at=now,
                ))
                self._write(envelope)
                return self._view(record)
        except DataSourceConflict:
            raise
        except Exception:  # noqa: BLE001 -- 存储异常可能含私密输入，只返回固定分类
            raise DataSourceUnavailable("数据源配置不可用") from None

    def record_test_result(
        self, tenant_id: TenantId, source_id: str, *, revision: int, actor_id: str,
        status: Literal["passed", "failed", "inconclusive"],
        scope: Literal["account", "sample"],
    ) -> DataSourceConfigurationView:
        """仅接受受信 Gateway 实际测试结果；旧版测试不可污染新凭证状态。"""
        self._check_tenant(tenant_id)
        if status not in {"passed", "failed", "inconclusive"} or scope not in {"account", "sample"}:
            raise DataSourceInputInvalid("数据源测试结果无效")
        try:
            with exclusive_profile_lock(self._directory):
                envelope = self._read()
                previous = envelope.records[source_id]
                if previous.revision != revision or not previous.configured:
                    raise DataSourceConflict("数据源配置已更新，请刷新")
                now = datetime.now(UTC)
                record = previous.model_copy(update={
                    "test_status": status, "tested_at": now, "last_test_scope": scope,
                })
                envelope.records[source_id] = record
                envelope.audit.append(_Audit(
                    tenant_id=self._tenant, actor_id=actor_id, source_id=source_id,
                    configuration_version=record.configuration_version or "",
                    revision=revision, action="test_recorded", occurred_at=now,
                    test_scope=scope, test_status=status,
                ))
                self._write(envelope)
                return self._view(record)
        except DataSourceConflict:
            raise
        except Exception:  # noqa: BLE001 -- 存储异常可能含私密输入，只返回固定分类
            raise DataSourceUnavailable("数据源配置不可用") from None
