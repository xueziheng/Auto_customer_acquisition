"""平台获客数据源管理；独立授权、完整字段与并发版本约束。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol

from domains.organization.data_source_browser import browser_view
from domains.organization.data_source_usage import usage_view
from domains.organization.platform_access import PlatformAccessService
from domains.organization.schemas import (
    DataSourceConfigurationView,
    DataSourceCredentialsDelete,
    DataSourceCredentialsUpdate,
    DataSourceNoteUpdate,
    DataSourceNoteView,
    DataSourcesView,
    DataSourceUsageCheck,
    DataSourceView,
)
from shared.authentication import AuthPrincipal
from shared.schemas.browser_probe import BrowserProbeObservation, BrowserSourceView
from shared.schemas.discovery_sources import DISCOVERY_SOURCES, get_discovery_source
from shared.schemas.identifiers import TenantId
from shared.schemas.provider_usage import ProviderUsageObservation


class DataSourceInputInvalid(Exception):
    """只携带固定错误，禁止转发输入和存储异常。"""


class DataSourceConflict(Exception):
    """旧版本不能覆盖新的凭证或恢复旧验证状态。"""


class DataSourceUnavailable(Exception):
    """配置未知或存储失败不能伪装为未配置。"""


class DataSourceConfigurationRepository(Protocol):
    """受信存储端口；调用和记录均绑定独立控制租户。"""

    def list_configurations(self, tenant_id: TenantId) -> dict[str, DataSourceConfigurationView]:
        """仅交付安全状态，不向领域返回凭证。"""
        ...

    def save_credentials(
        self, tenant_id: TenantId, source_id: str, actor_id: str,
        command: DataSourceCredentialsUpdate,
    ) -> DataSourceConfigurationView:
        """整组覆盖并在同一持久事务内创建新版本、撤销旧验证和写审计。"""
        ...

    def delete_credentials(
        self, tenant_id: TenantId, source_id: str, actor_id: str,
        command: DataSourceCredentialsDelete,
    ) -> DataSourceConfigurationView:
        """清除整组凭证，保留递增版本与审计，不保留旧凭证。"""
        ...


class DataSourceNotesRepository(Protocol):
    """备注使用独立存储与版本，不得触碰凭证、额度或测试状态。"""

    def list_notes(self, tenant_id: TenantId) -> dict[str, DataSourceNoteView]:
        """读取指定控制租户的人工备注，损坏时失败关闭。"""
        ...

    def save_note(
        self, tenant_id: TenantId, source_id: str, actor_id: str, command: DataSourceNoteUpdate,
    ) -> DataSourceNoteView:
        """锁内校验备注版本，保存文本或空白墓碑并记录修改人和时间。"""
        ...


class DataSourceUsageReader(Protocol):
    """额度能力按来源显式接入，领域和模型不读取凭证或访问网络。"""

    def supports(self, source_id: str) -> bool: ...

    def snapshot(self, tenant: TenantId, source_id: str, revision: int) -> ProviderUsageObservation | None: ...

    async def check(self, principal: AuthPrincipal, source_id: str, configuration: DataSourceConfigurationView) -> None: ...


class DataSourceBrowserReader(Protocol):
    def snapshot(self, tenant: TenantId) -> BrowserProbeObservation | None: ...

    async def check(self, principal: AuthPrincipal) -> None: ...


class DataSourceAdministrationService:
    """每次操作重新校验平台授权，不由用户名或前端角色授予权限。"""

    def __init__(
        self, control_tenant: TenantId, access: PlatformAccessService,
        repository: DataSourceConfigurationRepository,
        usage: DataSourceUsageReader | None = None,
        *, notes: DataSourceNotesRepository | None = None,
        browser: DataSourceBrowserReader | None = None,
    ) -> None:
        self._tenant, self._access, self._repository = control_tenant, access, repository
        self._usage = usage
        self._notes = notes
        self._browser = browser

    def _view(
        self, source_id: str, state: DataSourceConfigurationView, note: DataSourceNoteView | None = None,
    ) -> DataSourceView:
        definition = get_discovery_source(source_id)
        if definition is None:
            raise DataSourceInputInvalid("数据来源无效")
        supported = self._usage is not None and self._usage.supports(source_id)
        if note is None:
            note = self._notes.list_notes(self._tenant).get(source_id, DataSourceNoteView()) if self._notes else DataSourceNoteView()
        display = usage_view(None)
        if self._usage is not None and supported and state.configured:
            try:
                display = usage_view(self._usage.snapshot(self._tenant, source_id, state.revision))
            except DataSourceUnavailable:
                display = display.model_copy(update={"status": "unavailable"})
        api_method = bool(definition.credential_fields) or any(
            "API" in method and "逐站" not in method for method in definition.access_methods
        )
        status_kind: Literal["api", "session", "website"] = (
            "session" if source_id == "ungm" or definition.credential_mode == "managed_session"
            else "api" if api_method else "website"
        )
        browser = None
        if status_kind != "api":
            browser = BrowserSourceView()
            if self._browser is not None and source_id == "ungm":
                try:
                    browser = browser_view(self._browser.snapshot(self._tenant))
                except DataSourceUnavailable:
                    browser = BrowserSourceView(status="unavailable")
        return DataSourceView(
            status_kind=status_kind, browser=browser,
            can_check_browser=source_id == "ungm" and self._browser is not None,
            definition=definition, can_edit=bool(definition.credential_fields),
            can_edit_note=self._notes is not None, note=note,
            can_check_usage=supported, usage=display, **state.model_dump(),
        )

    async def list_sources(self, principal: AuthPrincipal) -> DataSourcesView:
        """当前平台管理员可读取目录；读取失败不能显示全部未配置。"""
        await self._access.authorize(principal)
        configurations = self._repository.list_configurations(self._tenant)
        notes = self._notes.list_notes(self._tenant) if self._notes else {}
        await self._access.authorize(principal)
        return DataSourcesView(generated_at=datetime.now(UTC), sources=tuple(
            self._view(
                definition.source_id, configurations.get(definition.source_id, DataSourceConfigurationView()),
                notes.get(definition.source_id, DataSourceNoteView()),
            )
            for definition in DISCOVERY_SOURCES
        ))

    async def save_note(
        self, principal: AuthPrincipal, source_id: str, command: DataSourceNoteUpdate,
    ) -> DataSourceNoteView:
        """当前平台授权下编辑所有已登记来源的人工备注，不调用任何供应商。"""
        await self._access.authorize(principal)
        if get_discovery_source(source_id) is None:
            raise DataSourceInputInvalid("数据来源无效")
        if self._notes is None:
            raise DataSourceUnavailable("来源备注暂不可用")
        note = self._notes.save_note(self._tenant, source_id, str(principal.user_id), command)
        await self._access.authorize(principal)
        return note

    async def save_credentials(
        self, principal: AuthPrincipal, source_id: str, command: DataSourceCredentialsUpdate,
    ) -> DataSourceView:
        """完整字段与当前授权通过后保存；保存不会访问供应商或启用业务能力。"""
        await self._access.authorize(principal)
        definition = get_discovery_source(source_id)
        if (
            definition is None or not definition.credential_fields
            or set(command.credentials) != {field.name for field in definition.credential_fields}
        ):
            raise DataSourceInputInvalid("数据源配置输入无效")
        state = self._repository.save_credentials(
            self._tenant, source_id, str(principal.user_id), command,
        )
        await self._access.authorize(principal)
        return self._view(source_id, state)

    async def delete_credentials(
        self, principal: AuthPrincipal, source_id: str, command: DataSourceCredentialsDelete,
    ) -> DataSourceView:
        """撤销平台保存的整组凭证；不声称撤销供应商网站颁发的密钥。"""
        await self._access.authorize(principal)
        definition = get_discovery_source(source_id)
        if definition is None or not definition.credential_fields:
            raise DataSourceInputInvalid("数据源配置输入无效")
        state = self._repository.delete_credentials(
            self._tenant, source_id, str(principal.user_id), command,
        )
        await self._access.authorize(principal)
        return self._view(source_id, state)

    async def check_usage(self, principal: AuthPrincipal, source_id: str, command: DataSourceUsageCheck) -> DataSourceView:
        """当前平台授权、已配置版本和来源能力均匹配时才委托免费额度检查。"""
        await self._access.authorize(principal)
        state = self._repository.list_configurations(self._tenant).get(source_id)
        if state is None or not state.configured or state.revision != command.expected_revision:
            raise DataSourceConflict("配置已更新")
        if self._usage is None or not self._usage.supports(source_id):
            raise DataSourceInputInvalid("额度查询尚未接入")
        await self._usage.check(principal, source_id, state)
        await self._access.authorize(principal)
        current = self._repository.list_configurations(self._tenant).get(source_id)
        if current is None or not current.configured or current.revision != state.revision:
            raise DataSourceConflict("配置已更新")
        return self._view(source_id, current)

    async def check_browser(self, principal: AuthPrincipal, source_id: str) -> DataSourceView:
        """当前平台管理员显式测试部署绑定会话；不提供任意来源浏览能力。"""
        await self._access.authorize(principal)
        if source_id != "ungm":
            raise DataSourceInputInvalid("浏览器测试尚未接入")
        if self._browser is None:
            raise DataSourceUnavailable("浏览器测试尚未启用")
        await self._browser.check(principal)
        await self._access.authorize(principal)
        state = self._repository.list_configurations(self._tenant).get(source_id, DataSourceConfigurationView())
        return self._view(source_id, state)
