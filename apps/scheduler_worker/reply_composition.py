"""完整回复原端口装配；每次业务动作重读受托员工，资源全部借用原runtime。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.model_client import StructuredJsonModelClient
from agent_runtime.qualification_agent.agent import QualificationAgent, ReplyClassifier
from agent_runtime.qualification_agent.openai_port import StructuredReplyModelPort
from domains.conversations.service import (
    ConversationsUnitOfWork,
    require_reply_internal_access,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope, ScopeLevel
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
)
from domains.outreach.service import OutreachService
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import EmployeeId, TenantId
from shared.schemas.quote_facts import QuoteEmployeeFact
from workflows.reply_qualification.ports import MessageContentReader, ReplyActionContext

from .adapters.reply_business_facts import TenantBoundReplyBusinessFactsReader
from .adapters.reply_current_access import CurrentReplyAccess
from .adapters.reply_evidence_reader import ConversationReplyEvidenceReader
from .adapters.reply_gateway_content import GatewayReplyContentReader
from .adapters.reply_opportunity_intake import DurableReplyOpportunityIntake
from .reply_actions import ComposedReplyActionPorts
from .reply_binding import ReplyRuntimeResources
from .runtime import ReplyQualificationComposition, SchedulerCoreServices


@dataclass(frozen=True)
class CurrentEmployeeReplyFactory:
    """配置只绑定真实员工ID与模型端口；角色从当前Employee服务读取。"""

    tenant_id: TenantId
    employee_id: EmployeeId
    model_client: StructuredJsonModelClient | None = None
    model: str = ""
    classifier: ReplyClassifier | None = None

    def __post_init__(self) -> None:
        if (self.classifier is None) == (self.model_client is None):
            raise ValueError("回复分类端口必须且只能配置一种")
        if self.classifier is None and not self.model.strip():
            raise ValueError("回复模型标识无效")

    def __call__(
        self,
        core: SchedulerCoreServices,
        outreach: OutreachService,
        resources: ReplyRuntimeResources,
    ) -> ReplyQualificationComposition:
        def uow(tenant: TenantId) -> ConversationsUnitOfWork:
            return cast(
                ConversationsUnitOfWork,
                SqlAlchemyConversationsUnitOfWork(
                    resources.sessions, tenant, now=resources.now
                ),
            )

        access = CurrentReplyAccess(
            self.tenant_id, self.employee_id, core.employee_scope, uow
        )
        content = GatewayReplyContentReader(
            self.tenant_id,
            resources.sessions,
            core.conversations,
            resources.bounded_raw_store,
            access,
            now=resources.now,
        )
        evidence = ConversationReplyEvidenceReader(uow)
        actions = CurrentEmployeeReplyActions(
            self.tenant_id,
            self.employee_id,
            core,
            outreach,
            resources,
            content,
            evidence,
        )
        classifier = self.classifier
        if classifier is None:
            model_client = self.model_client
            if model_client is None:
                raise ValueError("回复模型端口无效")
            classifier = QualificationAgent(
                self.model,
                StructuredReplyModelPort(model_client, self.model),
                None,
                None,
            )
        return ReplyQualificationComposition(
            classifier,
            content,
            CredentialMarkerGuard(),
            core.conversations,
            outreach,
            actions,
            access,
        )


@dataclass(frozen=True)
class CurrentEmployeeReplyActions:
    """借用每次调用独立的员工scope，业务规则仍由原ComposedReplyActionPorts及域执行。"""

    tenant_id: TenantId
    employee_id: EmployeeId
    core: SchedulerCoreServices
    outreach: OutreachService
    resources: ReplyRuntimeResources
    content: MessageContentReader
    evidence: ConversationReplyEvidenceReader

    async def _run(
        self,
        tenant: TenantId,
        context: ReplyActionContext,
        key: str,
        method: Literal[
            "route_bounce",
            "record_complaint",
            "request_handoff",
            "start_qualification",
            "extract_need_fields",
            "mark_future_restart",
            "create_follow_up",
            "intake_new_contact",
        ],
    ) -> None:
        if tenant != self.tenant_id:
            raise TenantIsolationViolation("回复动作跨租户执行")
        async with self.core.employee_scope(tenant) as employees:
            employee = await employees.get_employee(
                tenant,
                self.employee_id,
                actor=EmployeeActor(
                    "system:reply-actor", EmployeeScope.SYSTEM, "system"
                ),
            )
            fact = QuoteEmployeeFact.model_validate(
                {
                    "tenant_id": employee.tenant_id,
                    "employee_id": employee.employee_id,
                    "role": employee.role,
                    "is_active": employee.is_active,
                    "manager_id": employee.manager_id,
                    "team_id": employee.team_id,
                }
            )
            require_reply_internal_access(self.tenant_id, fact, action="qualify")
            opportunity_actor = OpportunityActor(
                str(employee.employee_id),
                OpportunityScope(level=ScopeLevel.TENANT),
                employee.role,
            )
            employee_actor = EmployeeActor(
                str(employee.employee_id), EmployeeScope.TENANT, employee.role
            )
            organization_actor = OrganizationActor(
                str(employee.employee_id),
                OrganizationScope(
                    level=OrganizationScopeLevel.TENANT, tenant_id=tenant
                ),
                employee.role,
            )
            business = TenantBoundReplyBusinessFactsReader(
                tenant_id=self.tenant_id,
                outreach=self.outreach,
                demand=self.core.demand,
                prospecting=self.core.prospecting,
                opportunities=self.resources.opportunities,
                opportunity_actor=opportunity_actor,
            )
            intake = DurableReplyOpportunityIntake(
                tenant_id=self.tenant_id,
                demand=self.core.demand,
                prospecting=self.core.prospecting,
                organization=self.core.organization,
                opportunities=self.resources.opportunities,
                employees=employees,
                evidence_reader=self.evidence,
                organization_actor=organization_actor,
                opportunity_actor=opportunity_actor,
                employee_actor=employee_actor,
            )
            actions = ComposedReplyActionPorts(
                tenant_id=self.tenant_id,
                evidence=self.evidence,
                business=business,
                content=self.content,
                demand=self.core.demand,
                opportunities=self.resources.opportunities,
                outreach=self.outreach,
                sending_identities=self.core.sending,
                conversations=self.core.conversations,
                opportunity_intake=intake,
            )
            await getattr(actions, method)(tenant, context, key)

    async def route_bounce(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "route_bounce")

    async def record_complaint(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "record_complaint")

    async def request_handoff(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "request_handoff")

    async def start_qualification(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "start_qualification")

    async def extract_need_fields(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "extract_need_fields")

    async def mark_future_restart(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "mark_future_restart")

    async def create_follow_up(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "create_follow_up")

    async def intake_new_contact(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "intake_new_contact")
