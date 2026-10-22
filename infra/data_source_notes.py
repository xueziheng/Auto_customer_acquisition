"""平台来源备注独立文件：租户绑定、原子写入与乐观版本，不读取凭证。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domains.organization.data_sources import (
    DataSourceConflict,
    DataSourceInputInvalid,
    DataSourceUnavailable,
)
from domains.organization.schemas import DataSourceNoteUpdate, DataSourceNoteView
from infra.pilot.config import (
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.discovery_sources import get_discovery_source
from shared.schemas.identifiers import TenantId


class _Audit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    tenant_id: str
    source_id: str
    actor_id: str
    revision: int = Field(strict=True, ge=1)
    action: Literal["note_updated", "note_cleared"]
    occurred_at: datetime


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    tenant_id: str
    schema_version: Literal[1] = 1
    records: dict[str, DataSourceNoteView] = Field(default_factory=dict, repr=False)
    audit: list[_Audit] = Field(default_factory=list)


class FileDataSourceNotesRepository:
    """只操作受信目录下 notes.json；价格等备注是人工记录，不是系统验收结论。"""

    def __init__(self, directory: Path, tenant_id: TenantId) -> None:
        if not directory.is_absolute() or ".." in directory.parts or not tenant_id:
            raise DataSourceUnavailable("来源备注暂不可用")
        self._directory, self._tenant = directory, tenant_id
        self._file = directory / "notes.json"

    def _check_tenant(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant:
            raise DataSourceUnavailable("来源备注暂不可用")

    def _read(self) -> _Envelope:
        checked_directory(self._directory)
        if not self._file.exists() and not self._file.is_symlink():
            return _Envelope(tenant_id=self._tenant)
        envelope = _Envelope.model_validate_json(private_read(self._file))
        if envelope.tenant_id != self._tenant or any(row.tenant_id != self._tenant for row in envelope.audit):
            raise DataSourceUnavailable("来源备注暂不可用")
        for source_id, note in envelope.records.items():
            if get_discovery_source(source_id) is None or note.revision < 1 or not note.updated_by or note.updated_at is None:
                raise DataSourceUnavailable("来源备注暂不可用")
            if DataSourceNoteUpdate(expected_revision=note.revision, text=note.text).text != note.text:
                raise DataSourceUnavailable("来源备注暂不可用")
        return envelope

    def list_notes(self, tenant_id: TenantId) -> dict[str, DataSourceNoteView]:
        """仅返回绑定租户的备注；缺文件为空，损坏或权限异常不能伪装成空。"""
        self._check_tenant(tenant_id)
        try:
            if not self._directory.exists() and not self._directory.is_symlink():
                checked_directory(self._directory.parent)
                return {}
            return self._read().records
        except Exception:  # noqa: BLE001 -- 不暴露人工备注、路径或底层错误
            raise DataSourceUnavailable("来源备注暂不可用") from None

    def save_note(
        self, tenant_id: TenantId, source_id: str, actor_id: str, command: DataSourceNoteUpdate,
    ) -> DataSourceNoteView:
        """备注单独递增版本；清空保留墓碑，凭证文件与原测试状态保持不变。"""
        self._check_tenant(tenant_id)
        if get_discovery_source(source_id) is None or not actor_id:
            raise DataSourceInputInvalid("来源备注输入无效")
        try:
            checked_directory(self._directory.parent)
            self._directory.mkdir(mode=0o700, exist_ok=True)
            with exclusive_profile_lock(self._directory):
                envelope = self._read()
                previous = envelope.records.get(source_id, DataSourceNoteView())
                if command.expected_revision != previous.revision:
                    raise DataSourceConflict("备注已更新，请刷新")
                now = datetime.now(UTC)
                note = DataSourceNoteView(
                    text=command.text, revision=previous.revision + 1, updated_at=now, updated_by=actor_id,
                )
                envelope.records[source_id] = note
                envelope.audit.append(_Audit(
                    tenant_id=self._tenant, source_id=source_id, actor_id=actor_id,
                    revision=note.revision, action="note_updated" if note.text else "note_cleared", occurred_at=now,
                ))
                private_write(self._file, json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False).encode())
                return note
        except DataSourceConflict:
            raise
        except Exception:  # noqa: BLE001 -- 不暴露人工备注、路径或底层错误
            raise DataSourceUnavailable("来源备注暂不可用") from None
