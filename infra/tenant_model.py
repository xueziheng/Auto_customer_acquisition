"""显式公司模型连接解析；权限由调用层先核验，策略版本与连接版本分离。"""
from infra.model_catalog import FileModelCatalog
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError


def resolve_task_model(settings: StandaloneModelSettings, identity: InvocationIdentity) -> StandaloneModelSettings:
    if settings.model_catalog_directory is None:
        return settings
    if str(identity.tenant_id) != settings.api_business_tenant or identity.configuration_version != settings.configuration_version:
        raise ModelGenerationError("configuration")
    catalog = FileModelCatalog(settings.model_catalog_directory, TenantId(settings.api_control_tenant))
    try:
        selected = catalog.for_run(str(identity.tenant_id), str(identity.run_id), f"{identity.user_id}/{identity.employee_id}", identity.configuration_version)
    except Exception:  # noqa: BLE001 -- 私有路径和原始存储错误不能交付业务模型。
        raise ModelGenerationError("configuration") from None
    view = selected.view
    return settings.model_copy(update={"model": view.model, "provider": view.provider,
        "api_base_url": view.base_url, "api_protocol": view.protocol, "api_key_file": selected.key_path,
        "api_connection_version": view.configuration_version, "secret_ref": "MODEL_API_KEY"})
