"""控制租户私有模型库；运行绑定只含引用，密钥由 Connector 解析。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from domains.organization.data_sources import DataSourceConflict, DataSourceUnavailable
from domains.organization.model_api import ModelApiUpdate, ModelApiView
from domains.organization.model_catalog import ModelCatalogEntry, ModelCatalogSave
from infra.model_api import FileModelApiRepository
from infra.pilot.config import (
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.identifiers import TenantId, new_id


@dataclass(frozen=True, repr=False)
class ResolvedModelConnection:
    entry_id: str
    view: ModelApiView
    key_path: Path

    @property
    def model(self) -> str:
        return self.view.model


class FileModelCatalog:
    def __init__(self, directory: Path, tenant_id: TenantId) -> None:
        if not directory.is_absolute() or ".." in directory.parts or not tenant_id:
            raise DataSourceUnavailable("模型库不可用")
        self.directory, self.tenant_id = directory, tenant_id

    def _read(self, tenant: TenantId) -> dict:
        if tenant != self.tenant_id:
            raise DataSourceUnavailable("模型库不可用")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        checked_directory(self.directory)
        (self.directory / "catalog-lock").mkdir(mode=0o700, exist_ok=True)
        path = self.directory / "catalog.json"
        data = json.loads(private_read(path)) if path.exists() else {"tenant_id": tenant, "entries": {}, "bindings": {}, "audit": []}
        if data["tenant_id"] != tenant:
            raise DataSourceUnavailable("模型库不可用")
        return data

    def _write(self, data: dict) -> None:
        private_write(self.directory / "catalog.json", json.dumps(data).encode())

    def model_store(self, entry_id: str) -> FileModelApiRepository:
        if not re.fullmatch(r"legacy|mc_[0-9A-HJKMNP-TV-Z]{26}", entry_id):
            raise DataSourceUnavailable("模型配置不存在")
        directory = self.directory if entry_id == "legacy" else self.directory / "connections" / entry_id
        if entry_id != "legacy":
            (self.directory / "connections").mkdir(mode=0o700, exist_ok=True)
        return FileModelApiRepository(directory, self.tenant_id)

    def _entry(self, data: dict, entry_id: str) -> dict:
        item = data["entries"].get(entry_id)
        if not item or item.get("deleted"):
            raise DataSourceUnavailable("模型配置不存在")
        return item

    def get(self, tenant: TenantId, entry_id: str) -> ModelCatalogEntry:
        data = self._read(tenant)
        item = self._entry(data, entry_id)
        view = self.model_store(entry_id).view(tenant)
        agent = item.get("agent", {}) if item.get("agent", {}).get("version") == view.configuration_version else {}
        return ModelCatalogEntry(**view.model_dump(), id=entry_id, name=item["name"],
            agent_test_status=agent.get("status", "not_run"), agent_tested_at=agent.get("at"),
            agent_failure_code=agent.get("failure"),
            used_by=tuple(t for t, b in data["bindings"].items() if b["model_id"] == entry_id))

    def entries(self, tenant: TenantId) -> tuple[ModelCatalogEntry, ...]:
        data = self._read(tenant)
        return tuple(self.get(tenant, key) for key, item in data["entries"].items() if not item.get("deleted"))

    def bindings(self, tenant: TenantId) -> dict:
        return self._read(tenant)["bindings"]

    def save(self, tenant: TenantId, actor: str, entry_id: str | None, update: ModelCatalogSave) -> ModelCatalogEntry:
        self._read(tenant)
        with exclusive_profile_lock(self.directory / "catalog-lock"):
            data = self._read(tenant)
            if entry_id is None:
                if update.expected_revision != 0 or len(data["entries"]) >= 100:
                    raise DataSourceConflict("模型数量或版本不允许新增")
                entry_id = new_id("mc")
                item: dict[str, Any] = {"name": update.name, "versions": {}}
            else:
                item = self._entry(data, entry_id)
            view = self.model_store(entry_id).save(tenant, actor, ModelApiUpdate.model_validate(update.model_dump(exclude={"name"})))
            item["name"] = update.name
            item["versions"][view.configuration_version] = view.model_dump(mode="json")
            data["entries"][entry_id] = item
            data["audit"].append({"actor": actor, "action": "save", "id": entry_id, "version": view.configuration_version})
            self._write(data)
        return self.get(tenant, entry_id)

    def record_agent_test(self, tenant: TenantId, entry_id: str, revision: int, status: str, failure: str | None) -> None:
        self._read(tenant)
        with exclusive_profile_lock(self.directory / "catalog-lock"):
            data = self._read(tenant)
            item = self._entry(data, entry_id)
            view = self.model_store(entry_id).view(tenant)
            if revision != view.revision or status not in {"passed", "failed"}:
                raise DataSourceConflict("配置已变化，请重新测试")
            item["agent"] = {"version": view.configuration_version, "status": status, "failure": failure, "at": datetime.now(UTC).isoformat()}
            self._write(data)

    def bind(self, tenant: TenantId, actor: str, company: str, entry_id: str, model_revision: int, expected_revision: int) -> None:
        self._read(tenant)
        with exclusive_profile_lock(self.directory / "catalog-lock"):
            data = self._read(tenant)
            item = self._entry(data, entry_id)
            view = self.model_store(entry_id).view(tenant)
            old = data["bindings"].get(company, {})
            agent = item.get("agent", {})
            if (old.get("revision", 0) != expected_revision or view.revision != model_revision
                or view.test_status != "passed" or agent.get("status") != "passed"
                or agent.get("version") != view.configuration_version):
                raise DataSourceConflict("请刷新配置，并通过 API 和 Agent 测试后选择")
            item["versions"][view.configuration_version] = view.model_dump(mode="json")
            data["bindings"][company] = {"revision": expected_revision + 1, "model_id": entry_id, "version": view.configuration_version, "model": view.model, "name": item["name"]}
            data["audit"].append({"actor": actor, "action": "bind", "company": company, "id": entry_id, "version": view.configuration_version, "at": datetime.now(UTC).isoformat()})
            self._write(data)

    def delete(self, tenant: TenantId, actor: str, entry_id: str, revision: int) -> None:
        self._read(tenant)
        with exclusive_profile_lock(self.directory / "catalog-lock"):
            data = self._read(tenant)
            item = self._entry(data, entry_id)
            if self.model_store(entry_id).view(tenant).revision != revision or any(b["model_id"] == entry_id for b in data["bindings"].values()):
                raise DataSourceConflict("配置已变化或仍有公司使用，请先切换公司模型")
            item["deleted"] = True
            data["audit"].append({"actor": actor, "action": "delete", "id": entry_id})
            self._write(data)

    def for_run(self, tenant: str, run_id: str, actor: str, policy_version: str) -> ResolvedModelConnection:
        """受信调用栈绑定任务版本；未知企业无默认回退，任务身份不能替换。"""
        self._read(self.tenant_id)
        digest = hashlib.sha256(json.dumps([tenant, run_id], separators=(",", ":")).encode()).hexdigest()
        pins = self.directory / "runs"
        pins.mkdir(mode=0o700, exist_ok=True)
        checked_directory(pins)
        path = pins / (digest + ".json")
        with exclusive_profile_lock(self.directory / "catalog-lock"):
            data = self._read(self.tenant_id)
            if path.exists():
                pin = json.loads(private_read(path))
                if (pin["tenant_id"], pin["run_id"], pin["actor"], pin["policy_version"]) != (tenant, run_id, actor, policy_version):
                    raise DataSourceUnavailable("任务模型绑定不匹配")
            else:
                binding = data["bindings"].get(tenant)
                if not binding:
                    raise DataSourceUnavailable("该公司尚未选择模型")
                pin = {"tenant_id": tenant, "run_id": run_id, "actor": actor, "policy_version": policy_version, "model_id": binding["model_id"], "version": binding["version"]}
                private_write(path, json.dumps(pin).encode())
            # 已开始任务可使用归档的版本；删除不扩大其他任务的访问范围。
            item = data["entries"].get(pin["model_id"], {})
            raw = item.get("versions", {}).get(pin["version"])
            if raw is None:
                raise DataSourceUnavailable("任务模型版本不存在")
            view = ModelApiView.model_validate(raw)
            return ResolvedModelConnection(pin["model_id"], view, self.model_store(pin["model_id"]).key_path(pin["version"]))

    def import_existing(self, settings, company: str, name: str = "DeepSeek") -> None:
        """显式迁移既有生效连接，仅引用原密钥文件，不读取密钥。"""
        self._read(self.tenant_id)
        with exclusive_profile_lock(self.directory / "catalog-lock"):
            data = self._read(self.tenant_id)
            if data["entries"]:
                return
            view = self.model_store("legacy").view(self.tenant_id)
            active = view.model_copy(update={"configuration_version": settings.configuration_version, "model": settings.model, "provider": settings.provider, "base_url": settings.api_base_url, "protocol": settings.api_protocol, "test_status": "passed" if settings.api_verified_at else "not_run", "tested_at": settings.api_verified_at})
            if not self.model_store("legacy").key_path(settings.configuration_version).is_file():
                raise DataSourceUnavailable("原模型密钥文件不存在")
            data["entries"]["legacy"] = {"name": name, "versions": {active.configuration_version: active.model_dump(mode="json"), view.configuration_version: view.model_dump(mode="json")}}
            data["bindings"][company] = {"revision": 1, "model_id": "legacy", "version": active.configuration_version, "model": active.model, "name": name}
            data["audit"].append({"actor": "system:migration", "action": "import", "company": company, "id": "legacy"})
            self._write(data)
