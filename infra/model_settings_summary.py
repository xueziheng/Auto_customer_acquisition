"""企业只读当前平台连接摘要；不创建运行绑定、读取密钥或修改历史配置。"""

from domains.assistant.schemas import ModelSettingsView
from domains.organization.data_sources import DataSourceUnavailable
from infra.model_catalog import FileModelCatalog
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId


class PlatformModelSummary:
    def __init__(self, settings: StandaloneModelSettings) -> None:
        self.settings = settings

    def __call__(self, tenant: TenantId, runtime: ModelSettingsView) -> ModelSettingsView:
        """当前新任务绑定独立于已开始任务pin；不可用时不显示旧SQL模型冒充当前。"""
        settings = self.settings
        if settings.model_catalog_directory is None:
            return runtime.model_copy(update={"model": settings.model, "provider": settings.provider})
        missing = runtime.model_copy(update={"model": None, "status": "missing", "can_probe": False})
        if not settings.api_control_tenant or settings.api_business_tenant != tenant:
            return missing
        try:
            view = FileModelCatalog(settings.model_catalog_directory, TenantId(settings.api_control_tenant)).bound_view(
                TenantId(settings.api_control_tenant), tenant
            )
        except (DataSourceUnavailable, OSError, ValueError, KeyError, TypeError):
            return missing
        if view is None or not view.configured:
            return missing
        return runtime.model_copy(update={
            "model": view.model, "provider": view.provider,
            "platform_connection_version": view.configuration_version,
            "status": "verified" if view.test_status == "passed" else "unverified",
            "can_probe": runtime.can_probe and view.test_status == "passed",
        })
