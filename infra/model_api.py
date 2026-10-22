"""显式私有模型配置目录，版本化密钥只由 Connector 解析。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from domains.organization.data_sources import (
    DataSourceConflict,
    DataSourceInputInvalid,
    DataSourceUnavailable,
)
from domains.organization.model_api import ModelApiCommand, ModelApiUpdate, ModelApiView
from infra.pilot.config import (
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.identifiers import TenantId, new_id

if TYPE_CHECKING:
    from infra.standalone.settings import StandaloneModelSettings


class FileModelApiRepository:
    def __init__(self, directory: Path, tenant_id: TenantId) -> None:
        if not directory.is_absolute() or ".." in directory.parts or not tenant_id:
            raise DataSourceUnavailable("模型配置不可用")
        self.directory, self.tenant_id = directory, tenant_id

    def key_path(self, version: str) -> Path:
        if not version or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-." for c in version) or "/" in version or ".." in version:
            raise DataSourceInputInvalid("配置版本无效")
        return self.directory / (version + ".key.json")

    def _read(self, tenant_id: TenantId) -> dict:
        if tenant_id != self.tenant_id:
            raise DataSourceUnavailable("模型配置不可用")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        checked_directory(self.directory)
        path = self.directory / "state.json"
        state = json.loads(private_read(path)) if path.exists() or path.is_symlink() else {"tenant_id": tenant_id, "view": {}, "audit": []}
        if state["tenant_id"] != tenant_id:
            raise DataSourceUnavailable("模型配置不可用")
        return state

    def _write(self, state: dict) -> None:
        private_write(self.directory / "state.json", json.dumps(state).encode())

    def view(self, tenant_id: TenantId) -> ModelApiView:
        return ModelApiView.model_validate(self._read(tenant_id)["view"])

    def save(self, tenant_id: TenantId, actor: str, update: ModelApiUpdate) -> ModelApiView:
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with exclusive_profile_lock(self.directory):
            state = self._read(tenant_id)
            old = ModelApiView.model_validate(state["view"])
            if old.revision != update.expected_revision or old.apply_status == "pending":
                raise DataSourceConflict("配置已更新或正在启用，请刷新后重试")
            key = update.api_key.get_secret_value() if update.api_key else None
            if key is None:
                if not old.configured or (old.provider, old.base_url) != (update.provider, update.base_url):
                    raise DataSourceInputInvalid("首次配置或更换服务商时请填写新的 API Key")
                key = json.loads(private_read(self.key_path(old.configuration_version)))["api_key"]
            version = new_id("mcv")
            private_write(self.key_path(version), json.dumps({"tenant_id": tenant_id, "configuration_version": version, "api_key": key}).encode())
            view = ModelApiView(
                **update.model_dump(exclude={"api_key", "expected_revision"}),
                configured=True, revision=old.revision + 1, configuration_version=version,
                updated_at=datetime.now(UTC), active_model=old.active_model,
                active_provider=old.active_provider, active_version=old.active_version,
            )
            state["view"] = view.model_dump(mode="json")
            state["audit"].append({"tenant_id": tenant_id, "actor": actor, "action": "save", "version": version, "at": view.updated_at.isoformat()})
            self._write(state)
            return view

    def record_test(self, tenant_id: TenantId, revision: int, status: str, failure: str | None) -> ModelApiView:
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with exclusive_profile_lock(self.directory):
            state = self._read(tenant_id)
            view = ModelApiView.model_validate(state["view"])
            if view.revision != revision or view.apply_status == "pending":
                raise DataSourceConflict("配置已更新，请重新测试")
            view = view.model_copy(update={"test_status": status, "failure_code": failure, "tested_at": datetime.now(UTC)})
            state["view"] = view.model_dump(mode="json")
            self._write(state)
            return view

    def request_apply(self, tenant_id: TenantId, actor: str, command: ModelApiCommand) -> ModelApiView:
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with exclusive_profile_lock(self.directory):
            state = self._read(tenant_id)
            view = ModelApiView.model_validate(state["view"])
            if view.revision != command.expected_revision or view.apply_status == "pending":
                raise DataSourceConflict("配置已更新或正在启用")
            if view.test_status != "passed":
                raise DataSourceInputInvalid("请先通过连接测试")
            view = view.model_copy(update={"apply_status": "pending"})
            state["view"] = view.model_dump(mode="json")
            state["audit"].append({"tenant_id": tenant_id, "actor": actor, "action": "apply", "version": view.configuration_version, "at": datetime.now(UTC).isoformat()})
            self._write(state)
            private_write(self.directory / "apply-request.json", json.dumps({"tenant_id": tenant_id, "revision": view.revision}).encode())
            return view

    def mark_applied(self, tenant_id: TenantId, revision: int, success: bool) -> None:
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with exclusive_profile_lock(self.directory):
            state = self._read(tenant_id)
            view = ModelApiView.model_validate(state["view"])
            if view.revision != revision:
                raise DataSourceConflict("配置已更新")
            changes = {"apply_status": "applied" if success else "failed"}
            if success:
                changes.update(active_model=view.model, active_provider=view.provider, active_version=view.configuration_version)
            state["view"] = view.model_copy(update=changes).model_dump(mode="json")
            self._write(state)

    def import_existing(self, tenant_id: TenantId, settings: StandaloneModelSettings, key: str) -> None:
        """部署方显式迁移原凭证，不能在 HTTP 路由自动发现环境文件。"""
        if self.view(tenant_id).revision:
            return
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with exclusive_profile_lock(self.directory):
            state = self._read(tenant_id)
            if state["view"]:
                return
            version = settings.configuration_version
            private_write(self.key_path(version), json.dumps({"tenant_id": tenant_id, "configuration_version": version, "api_key": key}).encode())
            state["view"] = ModelApiView(revision=1, configuration_version=version, model=settings.model,
                configured=True, updated_at=datetime.now(UTC), apply_status="applied", active_model=settings.model,
                active_provider="deepseek", active_version=version).model_dump(mode="json")
            state["audit"].append({"tenant_id": tenant_id, "actor": "system:migration", "action": "import", "version": version})
            self._write(state)


class ModelKeyFileResolver:
    """受信工厂精确绑定文件、租户和版本；只供 Connector 惰性读取。"""
    def __init__(self, path: Path, tenant: str, version: str, reference: str) -> None:
        self._path, self._tenant, self._version, self._reference = path, tenant, version, reference

    def resolve(self, reference: str) -> str:
        if reference != self._reference:
            raise ValueError("模型密钥引用无效")
        value = json.loads(private_read(self._path))
        if value["tenant_id"] != self._tenant or value["configuration_version"] != self._version or not isinstance(value["api_key"], str):
            raise ValueError("模型密钥绑定无效")
        return value["api_key"]
