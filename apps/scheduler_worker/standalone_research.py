"""独立后台研究装配；只用规范确认来源和公共服务。"""
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from apps.composition_support.model import build_model_composition
from apps.scheduler_worker.bootstrap import ResearchRuntimePorts
from apps.scheduler_worker.research_model_binding import (
    BoundResearchCapability,
    BoundResearchModelFactory,
    ResearchModelAuthority,
)
from apps.scheduler_worker.runtime import SchedulerCoreServices
from connectors.gmail.client import SecretResolver
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import Actor, EmployeeScope
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.research_model_binding import ResearchRunBindingReader
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.standalone.settings import StandaloneModelSettings
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import ModelProvider
from tool_gateway.repository import ToolGatewayUnitOfWork


def bind_research(core: SchedulerCoreServices, sessions: async_sessionmaker[AsyncSession], settings: StandaloneModelSettings,
                  resolver: SecretResolver, fingerprints: HmacFingerprintProvider, owner: str,
                  ports: ResearchRuntimePorts, provider_factory: Callable[[], ModelProvider] | None) -> ResearchRuntimePorts:
    lookup = Actor('system:research-model', EmployeeScope.SYSTEM, 'system')
    current = CurrentEmployeeIdentity(core.employee_scope, lookup)
    configuration = ModelConfigurationServiceImpl(SqlModelConfigurationRepository(sessions, fingerprints), current, now=lambda: datetime.now(UTC))
    authority = ResearchModelAuthority(ResearchRunBindingReader(sessions), core.directives, core.employee_scope, lookup, configuration)
    composition = build_model_composition(settings=settings, resolver=resolver, authority=authority, usage=SqlModelUsageRepository(sessions),
        ledger_factory=lambda tenant_id: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(sessions, tenant_id)), fingerprints=fingerprints,
        lease_owner=owner, lease_duration=timedelta(seconds=settings.limits.timeout_seconds+30), provider_factory=provider_factory)
    return replace(ports, capability=BoundResearchCapability(BoundResearchModelFactory(authority, composition.generator, settings.configuration_version), settings.model, settings.limits.max_output_tokens))
