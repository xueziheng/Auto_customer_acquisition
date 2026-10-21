"""完整回复原端口装配；每次业务动作重读受托员工，资源全部借用原runtime。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.model_client import StructuredJsonModelClient
from agent_runtime.qualification_agent.agent import QualificationAgent, ReplyClassifier
from agent_runtime.qualification_agent.openai_port import StructuredReplyModelPort
from domains.conversations.schemas import (
    ReplyCategory,
    ReplyWorkAction,
    ReplyWorkActionRequest,
)
from domains.conversations.service import (
    ConversationsUnitOfWork,
    reply_requires_owner_attention,
    require_reply_internal_access,
)
from domains.conversations.service_impl import ConversationServiceImpl
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
from infra.db.private_reply_test import PrivateReplyTestReader
from infra.db.qq_trial import SqlQQTrialGate
from infra.db.reply_retry import SqlReplyRetryRepository
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import EmployeeId, IdempotencyKey, MessageId, TenantId
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
    trial_gate: SqlQQTrialGate | None = None
    retries: SqlReplyRetryRepository | None = None
    recovery_only: bool = False

    def __post_init__(self) -> None:
        if self.trial_gate is not None:
            from apps.scheduler_worker.reply_model_binding import BoundReplyClassifier

            if not isinstance(
                self.classifier, BoundReplyClassifier
            ) or not self.classifier.uses_trial_gate(self.trial_gate):
                raise ValueError("受控回复必须接入同一持久模型门禁")
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
        async def commit(session: AsyncSession, message: MessageId | None) -> None:
            if (
                self.retries is not None
                and message is not None
                and await self.retries.exists(self.tenant_id, message)
            ):
                await self.retries.commit_message(session, self.tenant_id, message)
            elif self.trial_gate is not None:
                await self.trial_gate.commit_message(session, message)

        if self.retries is not None:
            if resources.bind_classification_commit is None:
                raise ValueError("回复恢复必须绑定原会话提交门禁")
            resources.bind_classification_commit(commit)

        def uow(tenant: TenantId) -> ConversationsUnitOfWork:
            return cast(
                ConversationsUnitOfWork,
                SqlAlchemyConversationsUnitOfWork(
                    resources.sessions,
                    tenant,
                    now=resources.now,
                    trial_commit=commit if self.trial_gate or self.retries else None,
                ),
            )

        if (
            resources.trial_gate is not None
            and resources.trial_gate is not self.trial_gate
        ):
            raise ValueError("原运行资源与回复门禁不一致")
        if self.retries is None and self.trial_gate is not None and resources.trial_gate is None:
            core = replace(
                core, conversations=ConversationServiceImpl(uow, now=resources.now)
            )
        access = CurrentReplyAccess(
            self.tenant_id,
            self.employee_id,
            core.employee_scope,
            uow,
            self.trial_gate,
            self.retries,
            self.recovery_only,
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
            "review_reply",
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
            private_test = await PrivateReplyTestReader(
                self.resources.sessions
            ).matches(
                tenant,
                context.account_id,
                context.contact_point_id,
                context.enrollment_id,
            )
            if private_test and method not in {
                "route_bounce",
                "record_complaint",
                "review_reply",
            }:
                classification = (
                    await self.core.conversations.get_effective_reply_category(
                        tenant, context.message_id
                    )
                )
                if classification is None:
                    raise ValueError("私有测试回复分类不存在")
                if reply_requires_owner_attention(ReplyCategory(classification)):
                    # 所有正向测试回复合为原owner工作项；不晋升Need或创建Handoff。
                    await self.core.conversations.enqueue_reply_work_action(
                        tenant,
                        ReplyWorkActionRequest(
                            message_id=context.message_id,
                            outbound_message_id=context.outbound_message_id,
                            enrollment_id=context.enrollment_id,
                            account_id=context.account_id,
                            contact_point_id=context.contact_point_id,
                            action=ReplyWorkAction.START_QUALIFICATION,
                            idempotency_key=IdempotencyKey(
                                f"reply:start_qualification:{context.message_id}"
                            ),
                        ),
                    )
                return
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

    async def review_reply(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        await self._run(tenant_id, context, idempotency_key, "review_reply")

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
