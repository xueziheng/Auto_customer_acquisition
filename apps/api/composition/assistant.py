"""API 独有会话与配置装配；模型请求留给 scheduler。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies import ConfiguredApiDependencies
from apps.composition_support.assistant import build_assistant_core
from domains.assistant.service import AssistantService, ModelConfigurationService
from domains.demand.service import DemandService
from infra.standalone.runtime import ModelRuntimeLifecycle
from infra.standalone.settings import StandaloneModelSettings
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider


@dataclass(frozen=True)
class AssistantApiComposition:
    service: AssistantService
    configuration: ModelConfigurationService
    lifecycle: ModelRuntimeLifecycle


def build_api_assistant(
    factory: async_sessionmaker[AsyncSession],
    dependencies: ConfiguredApiDependencies,
    *,
    tenant_id: TenantId,
    settings: StandaloneModelSettings,
    fingerprints: HmacFingerprintProvider,
) -> AssistantApiComposition:
    if dependencies.demand_radar is None or dependencies.run_audit is None:
        raise ValidationError("会话当前事实端口未配置")
    core = build_assistant_core(
        factory=factory,
        settings=settings,
        employees=dependencies.employees,
        lookup=dependencies.employee_lookup_actor,
        opportunities=dependencies.opportunities,
        demand=cast(DemandService, dependencies.demand_radar),
        run_audit=dependencies.run_audit,
        fingerprints=fingerprints,
        now=lambda: datetime.now(UTC),
    )
    return AssistantApiComposition(
        core.service,
        core.configuration,
        ModelRuntimeLifecycle(core.repository, settings, tenant_id, "api"),
    )
