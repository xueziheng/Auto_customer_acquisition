"""公开候选运行能力投影；配置和API验证都不能冒充完整采集路径。"""

from collections.abc import Awaitable, Callable
from typing import Literal

from connectors.search_contracts import SearchCostStatus
from domains.assistant.schemas import ModelConfigurationSnapshot
from domains.prospecting.service import AcquisitionJob, AcquisitionWorker, SourceState
from shared.schemas.discovery_sources import DISCOVERY_SOURCES
from shared.schemas.identifiers import TenantId
from tool_gateway.free_search_contracts import SearchQuotaRepository

WEB_INVENTORY_SOURCES = frozenset(
    {
        "company_websites",
        "industry_directories",
        "trade_associations",
        "chambers",
        "trade_fairs",
        "dealer_networks",
        "company_procurement",
        "public_procurement_pages",
        "company_news",
        "linkedin_public",
        "trade_record_indexes",
    }
)


class InventorySources:
    def __init__(
        self,
        *,
        tenant_id: TenantId,
        backend: AcquisitionWorker,
        quota: SearchQuotaRepository,
        configured: bool,
        model_snapshot: Callable[[TenantId], Awaitable[ModelConfigurationSnapshot | None]] | None = None,
        probe: Callable[
            [AcquisitionJob, Callable[[], Awaitable[None]]], Awaitable[None]
        ]
        | None = None,
    ) -> None:
        self.probe = probe
        self.model_snapshot = model_snapshot
        self.tenant_id, self.backend, self.quota, self.configured = (
            tenant_id,
            backend,
            quota,
            configured,
        )

    async def prepare(
        self, job: AcquisitionJob, guard: Callable[[], Awaitable[None]]
    ) -> None:
        """仅后台新任务经Gateway重新核实未知用量；API列表始终只读。"""
        if job.tenant_id != self.tenant_id:
            raise ValueError("来源租户不匹配")
        snapshot = await self.quota.snapshot() if self.configured else None
        # ADR0076：按量状态未知，不否认已独立核实的套餐内免费额度。
        if (
            self.configured
            and self.probe is not None
            and (
                snapshot is None
                or (
                    snapshot.cost_status is SearchCostStatus.UNKNOWN
                    and not snapshot.included_credits_free
                )
            )
        ):
            await guard()
            await self.probe(job, guard)

    async def list_sources(self, tenant_id: TenantId) -> tuple[SourceState, ...]:
        """只读安全配额快照和真实历史；从不加载来源凭证或调用Provider。"""
        if tenant_id != self.tenant_id:
            raise ValueError("来源租户不匹配")
        history = {s.source_id: s for s in await self.backend.source_history(tenant_id)}
        snapshot = await self.quota.snapshot() if self.configured else None
        model = await self.model_snapshot(tenant_id) if self.model_snapshot else None
        states = []
        for source in DISCOVERY_SOURCES:
            if source.selection != "selected":
                continue
            availability: Literal[
                "available",
                "not_configured",
                "not_integrated",
                "permission_denied",
                "quota_exhausted",
                "unhealthy",
            ] = "not_integrated"
            cost: Literal["free", "included", "paid", "unknown"] = "unknown"
            if source.source_id in WEB_INVENTORY_SOURCES or source.source_id == "tavily":
                availability = "not_configured"
                if self.configured:
                    if (
                        snapshot is None
                        or (
                            snapshot.cost_status is SearchCostStatus.UNKNOWN
                            and not snapshot.included_credits_free
                        )
                    ):
                        availability = "unhealthy"
                    elif (
                        snapshot.cost_status is SearchCostStatus.FREE
                        or snapshot.included_credits_free
                    ):
                        cost = "free"
                        availability = (
                            "available" if snapshot.remaining > 0 else "quota_exhausted"
                        )
                    else:
                        availability = "permission_denied"
            capability: Literal["not_integrated", "web_discovery", "search_backend", "model_runner"] = "not_integrated"
            if source.source_id in WEB_INVENTORY_SOURCES:
                capability = "web_discovery"
            elif source.source_id == "tavily":
                capability = "search_backend"
            elif source.source_id == "codex_cli":
                capability = "model_runner"
                # 非秘密配置快照仅证明配置与数据授权，不能证明当前账号可运行。
                availability = "not_configured" if model is None else (
                    "unhealthy" if model.model_data_export_enabled else "permission_denied"
                )
            prior = history.get(source.source_id)
            states.append(
                SourceState(
                    source_id=source.source_id,
                    availability=availability,
                    cost_class=cost,
                    inventory_capability=capability,
                    relevant=source.source_id in {"company_websites", "dealer_networks"},
                    valid_new_count=prior.valid_new_count if prior else 0,
                    attempts=prior.attempts if prior else 0,
                    last_used_at=prior.last_used_at if prior else None,
                )
            )
        return tuple(states)
