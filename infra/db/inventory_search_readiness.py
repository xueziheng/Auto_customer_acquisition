"""只读当前平台绑定和原搜索账本；API与worker共享投影，不创建探针或run。"""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.organization.model_api import ModelApiView
from infra.model_catalog import FileModelCatalog
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId
from shared.schemas.search_backend import InventorySearchPolicy, SearchBackendReadiness

from .prospect_inventory_tables import AcquisitionJobRow
from .tables import ToolCallRow


class InventorySearchReadinessReader:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant: TenantId,
        settings: StandaloneModelSettings,
        *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.factory, self.tenant, self.settings = factory, tenant, settings
        self.now = now

    def _connection(self) -> ModelApiView | None:
        settings = self.settings
        if (
            settings.api_business_tenant != str(self.tenant)
            or settings.api_control_tenant is None
        ):
            return None
        if settings.model_catalog_directory is not None:
            return FileModelCatalog(
                settings.model_catalog_directory, TenantId(settings.api_control_tenant)
            ).bound_view(TenantId(settings.api_control_tenant), self.tenant)
        if settings.api_connection_version is None or settings.api_key_file is None:
            return None
        return ModelApiView(
            configuration_version=settings.api_connection_version,
            provider=settings.provider,
            base_url=settings.api_base_url,
            configured=True,
            test_status="passed" if settings.api_verified_at is not None else "not_run",
        )

    async def read(
        self, tenant: TenantId, policy: InventorySearchPolicy
    ) -> SearchBackendReadiness:
        """仅SELECT同tenant/当前连接/完整政策的最新执行，不继承旧版本成功。"""
        if tenant != self.tenant or policy.backend_id != "deepseek":
            raise ValueError("搜索状态租户或后端不匹配")
        result = SearchBackendReadiness(policy_version=policy.version)
        try:
            view = self._connection()
        except Exception:  # noqa: BLE001 -- 私有目录异常只投影固定失败，不能输出原文。
            return result.model_copy(update={"connection_status": "failed"})
        if view is None or not view.configured or view.configuration_version is None:
            return result
        result = result.model_copy(
            update={"connection_version": view.configuration_version}
        )
        if view.provider != "deepseek" or view.base_url not in {
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
        }:
            return result
        result = result.model_copy(update={"connection_status": view.test_status})
        binding = {
            "backend_id": "deepseek",
            "connection_version": view.configuration_version,
            "policy_version": policy.version,
            "policy": policy.model_dump(mode="json"),
        }
        async with self.factory() as session:
            call = (
                await session.execute(
                    select(ToolCallRow.status, ToolCallRow.error_category, ToolCallRow.lease_expires_at)
                    .join(
                        AcquisitionJobRow,
                        (AcquisitionJobRow.tenant_id == ToolCallRow.tenant_id)
                        & (AcquisitionJobRow.job_id == ToolCallRow.run_id),
                    )
                    .where(
                        AcquisitionJobRow.tenant_id == tenant,
                        ToolCallRow.tenant_id == tenant,
                        AcquisitionJobRow.search_binding.contains(binding),
                        ToolCallRow.tool_id == "web.search",
                        ToolCallRow.tool_version == "v1.inventory.deepseek",
                        ToolCallRow.status.in_(
                            (
                                "executing",
                                "succeeded",
                                "failed_permanent",
                                "failed_transient",
                            )
                        ),
                    )
                    .order_by(
                        ToolCallRow.created_at.desc(), ToolCallRow.tool_call_id.desc()
                    )
                    .limit(1)
                )
            ).first()
        if call is None:
            return result
        if call.status == "executing":
            # 在途且租约有效不等于失败；否则当前请求自己的guard会拒绝自己。
            state = "running" if call.lease_expires_at is not None and call.lease_expires_at > self.now() else "unknown"
        elif call.status == "succeeded":
            state = "succeeded"
        elif call.error_category in {"provider_transient", "reconciliation_required", "unexpected"}:
            state = "unknown"
        else:
            state = "failed"
        return result.model_copy(update={"search_execution_state": state})
