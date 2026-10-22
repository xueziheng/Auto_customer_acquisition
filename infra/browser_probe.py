"""按控制租户持久化无凭证的浏览器检查结果。"""
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from domains.organization.data_sources import DataSourceUnavailable
from infra.pilot.config import (
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.browser_probe import BrowserProbeObservation
from shared.schemas.identifiers import TenantId


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str
    actor_id: str
    tool_call_id: str | None
    observation: BrowserProbeObservation


class FileBrowserProbeRepository:
    """独立文件不触碰 API Key 或浏览器凭证，重启后保留测试时间。"""
    def __init__(self, directory: Path, tenant: TenantId) -> None:
        self._directory, self._tenant = directory, tenant

    def read(self, tenant: TenantId) -> BrowserProbeObservation | None:
        if tenant != self._tenant:
            raise DataSourceUnavailable("浏览器检查记录不可用")
        try:
            path = self._directory / "browser-ungm.json"
            if not path.exists() and not path.is_symlink():
                return None
            checked_directory(self._directory)
            row = _Record.model_validate_json(private_read(path))
            if row.tenant_id != tenant:
                raise ValueError("tenant_mismatch")
            return row.observation
        except Exception:  # noqa: BLE001 -- 私有文件异常只能返回固定错误
            raise DataSourceUnavailable("浏览器检查记录不可用") from None

    def record(self, tenant: TenantId, actor: str, tool_call_id: str | None, observation: BrowserProbeObservation) -> None:
        if tenant != self._tenant:
            raise DataSourceUnavailable("浏览器检查记录不可用")
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with exclusive_profile_lock(self._directory):
                previous = self.read(tenant)
                if previous is not None and previous.checked_at > observation.checked_at:
                    return
                private_write(self._directory / "browser-ungm.json", _Record(
                    tenant_id=tenant, actor_id=actor, tool_call_id=tool_call_id, observation=observation,
                ).model_dump_json().encode())
        except Exception:  # noqa: BLE001 -- 私有文件异常只能返回固定错误
            raise DataSourceUnavailable("浏览器检查记录不可用") from None


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str
    profile: Literal["ungm-1287509"]
    enabled: bool


def ungm_probe_enabled(directory: Path | None, tenant: TenantId) -> bool:
    """只接受部署创建的私有绑定；缺少或损坏时关闭测试入口。"""
    if directory is None:
        return False
    try:
        path = directory / "ungm-browser-binding.json"
        if not path.exists() and not path.is_symlink():
            return False
        checked_directory(directory)
        binding = _Binding.model_validate_json(private_read(path))
        return binding.tenant_id == tenant and binding.enabled
    except Exception:  # noqa: BLE001 -- 部署文件故障只关闭能力
        return False
