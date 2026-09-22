"""ADR0071：公开域服务的 scope 映射与私有会话构造，无进程副作用。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.assistant.context import AssistantContextBuilder, HistoryProjector
from agent_runtime.assistant.reads import (
    BusinessReads,
    CurrentEmployeeIdentity,
    EmployeeScopeFactory,
)
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from domains.assistant.schemas import AssistantActor
from domains.assistant.service_impl import (
    AssistantServiceImpl,
    ModelConfigurationServiceImpl,
)
from domains.demand.service import DemandService
from domains.employees.service import Actor as EmployeeActor
from domains.opportunities.service import OpportunityService
from infra.db.assistant import SqlAssistantRepository
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from workflows.assistant.reads import AssistantRunReads
from workflows.engine.audit import RunAuditService


class ModelReadyGuard:
    def __init__(self, service: ModelConfigurationServiceImpl, version: str) -> None:
        self._service, self._version = service, version

    async def check(self, actor: AssistantActor) -> None:
        await self._service.authorize(actor, self._version, probe=False)


@dataclass(frozen=True)
class AssistantCore:
    service: AssistantServiceImpl
    configuration: ModelConfigurationServiceImpl
    repository: SqlModelConfigurationRepository
    identity: CurrentEmployeeIdentity
    reads: BusinessReads
    context: AssistantContextBuilder


def build_assistant_core(
    *,
    factory: async_sessionmaker[AsyncSession],
    settings: StandaloneModelSettings,
    employees: EmployeeScopeFactory,
    lookup: EmployeeActor,
    opportunities: OpportunityService,
    demand: DemandService,
    run_audit: RunAuditService,
    fingerprints: HmacFingerprintProvider,
    now: Callable[[], datetime],
) -> AssistantCore:
    identity = CurrentEmployeeIdentity(employees, lookup)
    configuration_repo = SqlModelConfigurationRepository(factory, fingerprints, now=now)
    configuration = ModelConfigurationServiceImpl(configuration_repo, identity, now=now)

    @asynccontextmanager
    async def opportunity_scope(
        tenant_id: TenantId,
    ) -> AsyncIterator[OpportunityService]:
        del tenant_id
        yield opportunities

    @asynccontextmanager
    async def demand_scope(tenant_id: TenantId) -> AsyncIterator[DemandService]:
        del tenant_id
        yield demand

    reads = BusinessReads(
        identity,
        opportunity_scope,
        demand_scope,
        AssistantRunReads(identity, run_audit),
    )
    repository = SqlAssistantRepository(factory)
    projector = HistoryProjector(identity, reads, repository)
    service = AssistantServiceImpl(
        repository,
        identity,
        CredentialMarkerGuard(),
        fingerprints,
        projector,
        ModelReadyGuard(configuration, settings.configuration_version),
    )
    return AssistantCore(
        service,
        configuration,
        configuration_repo,
        identity,
        reads,
        AssistantContextBuilder(
            identity,
            reads,
            repository,
            projector,
            configuration_version=settings.configuration_version,
            max_bytes=settings.limits.max_input_bytes,
        ),
    )
