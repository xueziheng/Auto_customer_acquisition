"""当前确认来源绑定模型；同一研究步骤永远使用同一调用序号。"""
from typing import Protocol

from agent_runtime.assistant.reads import CurrentEmployeeIdentity, EmployeeScopeFactory
from agent_runtime.base import AgentTask, ChangeSet
from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
from agent_runtime.demand_intelligence.model_port import (
    StructuredDemandIntelligenceModelPort,
)
from agent_runtime.gateway_model import GatewayJsonModelClient
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from domains.assistant.schemas import AssistantActor
from domains.assistant.service import ModelConfigurationService
from domains.directives.service import DirectiveService
from domains.employees.permissions import Actor
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, RunId, TenantId
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelGenerationPort,
)


def validate_binding(run_id: str, employee_id: str) -> None:
    if not run_id or not employee_id or run_id != run_id.strip() or employee_id != employee_id.strip():
        raise ValidationError('研究模型缺少受信运行身份')


class RunBindingReader(Protocol):
    async def proposal(self, tenant_id: TenantId, run_id: RunId) -> str: ...


class ResearchModelAuthority:
    def __init__(self, runs: RunBindingReader, directives: DirectiveService,
                 employees: EmployeeScopeFactory, lookup: Actor,
                 configuration: ModelConfigurationService) -> None:
        self._runs, self._directives, self._employees, self._lookup, self._configuration = runs, directives, employees, lookup, configuration
        self._identity = CurrentEmployeeIdentity(employees, lookup)

    async def actor(self, tenant: TenantId, run: RunId) -> AssistantActor:
        proposal_id = await self._runs.proposal(tenant, run)
        proposal = await self._directives.get_proposal(tenant, proposal_id)
        employee_id = proposal.decided_by_id
        if proposal.state != 'confirmed' or not employee_id:
            raise ModelGenerationError('permission')
        validate_binding(run, employee_id)
        async with self._employees(tenant) as employees:
            employee = await employees.get_employee(tenant, EmployeeId(employee_id), actor=self._lookup)
        if not employee.user_id:
            raise ModelGenerationError('permission')
        actor = AssistantActor(tenant_id=tenant, employee_id=employee.employee_id, user_id=employee.user_id)
        await self._identity.require_admin(actor)
        plan = await self._directives.get_confirmed_discovery_plan(tenant, proposal_id, employee.employee_id)
        if plan.execution_mode != 'research_only':
            raise ModelGenerationError('permission')
        return actor

    async def check(self, identity: InvocationIdentity) -> None:
        if identity.capability != 'research' or identity.turn_id is not None or identity.sequence != 0:
            raise ModelGenerationError('permission')
        actor = await self.actor(identity.tenant_id, identity.run_id)
        if actor.employee_id != identity.employee_id or actor.user_id != identity.user_id:
            raise ModelGenerationError('permission')
        await self._configuration.authorize(actor, identity.configuration_version, probe=False)


class BoundResearchModelFactory:
    def __init__(self, authority: ResearchModelAuthority, generator: ModelGenerationPort, configuration_version: str) -> None:
        self._authority, self._generator, self._version = authority, generator, configuration_version

    async def actor(self, tenant_id: TenantId, run_id: RunId) -> AssistantActor:
        return await self._authority.actor(tenant_id, run_id)

    async def for_step(self, tenant_id: TenantId, run_id: RunId, step_name: str, sequence: int) -> GatewayJsonModelClient:
        if step_name != 'execute_search' or type(sequence) is not int or sequence != 0:
            raise ModelGenerationError('permission')
        actor = await self._authority.actor(tenant_id, run_id)
        identity = InvocationIdentity(**actor.model_dump(), run_id=run_id, capability='research', configuration_version=self._version, sequence=sequence)
        await self._authority.check(identity)
        return GatewayJsonModelClient(identity, self._generator)


class BoundResearchCapability:
    def __init__(self, factory: BoundResearchModelFactory, model: str, output_limit: int) -> None:
        self._factory, self._model, self._limit = factory, model, output_limit

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        client = await self._factory.for_step(task.tenant_id, task.run_id, 'execute_search', 0)
        # 身份来自确认提案；任务本身也必须与该确认来源一致。
        actor = await self._factory.actor(task.tenant_id, task.run_id)
        if task.acting_user != actor.user_id:
            raise ModelGenerationError('permission')
        agent = DemandIntelligenceAgent(self._model, StructuredDemandIntelligenceModelPort(client, self._model, max_output_tokens=self._limit), None, CredentialMarkerGuard())
        return await agent.run(task, context)
