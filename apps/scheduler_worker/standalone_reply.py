"""独立入口的回复模型接线；资源和生命周期归原 scheduler 所有。"""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from agent_runtime.qualification_agent.agent import (
    ReplyClassificationResult,
    ReplyFieldCandidate,
)
from apps.composition_support.model import (
    build_model_composition,
    configured_codex_provider,
)
from connectors.gmail.client import SecretResolver
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import Actor, EmployeeScope
from domains.outreach.service import OutreachService
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.qq_trial import SqlQQTrialGate
from infra.db.reply_model_binding import SqlReplyRunBindingReader
from infra.db.reply_retry import SqlReplyRetryRepository
from infra.db.reply_retry_budget import SqlReplyRetryBudget
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.standalone.settings import StandaloneModelSettings, deployment_verification
from infra.tenant_model import resolve_task_model
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId, new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelGenerationPort,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import ModelProvider
from tool_gateway.qq_trial_model import TrialModelPorts, TrialProviderBudget
from tool_gateway.reply_retry_model import ReplyRetryModelGenerator
from tool_gateway.repository import ToolGatewayUnitOfWork

from .reply_binding import ReplyRuntimeResources
from .reply_composition import CurrentEmployeeReplyFactory
from .reply_model_binding import BoundReplyClassifier, ReplyModelAuthority
from .runtime import ReplyQualificationComposition, SchedulerCoreServices


def bind_reply(
    core: SchedulerCoreServices,
    outreach: OutreachService,
    resources: ReplyRuntimeResources,
    *,
    tenant_id: TenantId,
    employee_id: EmployeeId | None,
    settings: StandaloneModelSettings,
    resolver: SecretResolver,
    fingerprints: HmacFingerprintProvider,
    owner: str,
    mailbox_directory: Path | None = None,
    provider_factory: Callable[[], ModelProvider] | None = None,
    trial_gate: SqlQQTrialGate | None = None,
    trial_provider_factory: Callable[
        [TrialProviderBudget, InvocationIdentity], ModelProvider
    ]
    | None = None,
) -> ReplyQualificationComposition:
    """只借用原进程的 SQL/原件/域端口，不启动第二套循环或模型心跳。"""
    if not settings.reply_enabled:
        raise ValueError("回复模型必须显式启用")
    sessions = resources.sessions
    lookup = Actor("system:reply-model", EmployeeScope.SYSTEM, "system")
    current = CurrentEmployeeIdentity(core.employee_scope, lookup)
    configuration = ModelConfigurationServiceImpl(
        SqlModelConfigurationRepository(
            sessions,
            fingerprints,
            now=resources.now,
            deployment_verification=deployment_verification(settings),
            deployment_provider=settings.provider,
        ),
        current,
        now=resources.now,
    )
    retries = SqlReplyRetryRepository(sessions, configuration, now=resources.now)
    from apps.scheduler_worker.adapters.dialogue_model_access import (
        OrdinaryReplyModelAccess,
    )
    from domains.products.permissions import Phase2ProductAuthorizer
    from domains.products.service import ProductsUnitOfWork
    from domains.products.service_impl import ProductServiceImpl
    from infra.aliyun_mail_store import AliyunMailStore
    from infra.business_mail_binding import CurrentBusinessMailboxBinding
    from infra.db.business_mail import SqlBusinessMailRepository
    from infra.db.dialogue_owner_access import SqlDialogueOwnerAccess
    from infra.db.dialogue_source import SqlDialogueSources
    from infra.db.products_uow import SqlAlchemyProductsUnitOfWork
    from workflows.reply_qualification.dialogue_authority import (
        CurrentOrdinaryDialogueAccess,
    )

    from .dialogue_models import OrdinaryDialogueModels
    sources = SqlDialogueSources(sessions, tenant_id)
    dialogue = None
    if mailbox_directory is not None and settings.qq_reply is None:
        mailbox_binding = CurrentBusinessMailboxBinding(AliyunMailStore(mailbox_directory))
        mailbox = SqlBusinessMailRepository(sessions, mailbox_binding)
        products = ProductServiceImpl(lambda tenant: cast(ProductsUnitOfWork, SqlAlchemyProductsUnitOfWork(sessions, tenant)),
            Phase2ProductAuthorizer(tenant_id), now=resources.now)
        dialogue = CurrentOrdinaryDialogueAccess(SqlDialogueOwnerAccess(sessions, tenant_id, mailbox),
            outreach, products, core.prospecting)
    authority = ReplyModelAuthority(
        SqlReplyRunBindingReader(sessions),
        core.employee_scope,
        lookup,
        configuration,
        tenant_id=tenant_id,
        employee_id=employee_id,
        model=settings.model,
        retries=retries,
        ordinary=OrdinaryReplyModelAccess(sources, dialogue) if dialogue else None,
        resolved_model=lambda identity: resolve_task_model(settings, identity).model,
        recovery_only=bool(settings.qq_reply and settings.qq_reply.recovery_only),
    )
    model = build_model_composition(
        settings=settings,
        resolver=resolver,
        authority=authority,
        usage=SqlModelUsageRepository(sessions),
        ledger_factory=lambda tenant: cast(
            ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(sessions, tenant)
        ),
        fingerprints=fingerprints,
        lease_owner=owner,
        lease_duration=timedelta(seconds=settings.limits.timeout_seconds + 30),
        provider_factory=provider_factory if trial_gate is None else None,
        trial_gate=trial_gate,
        trial_provider_factory=trial_provider_factory,
    )

    ordinary_models = OrdinaryDialogueModels(sessions, settings, dialogue, authority, fingerprints, owner) if dialogue else None

    async def select_ordinary(identity: InvocationIdentity, message: MessageId) -> tuple[str, ModelGenerationPort] | None:
        source = await sources.for_message(tenant_id, message)
        if source is None:
            return None
        if ordinary_models is None:
            raise ModelGenerationError("permission")
        return await ordinary_models.select(identity, message)

    def retry_generator(
        identity: InvocationIdentity, message: MessageId
    ) -> ModelGenerationPort:
        selected = resolve_task_model(settings, identity)
        bounded = selected.model_copy(
            update={
                "limits": selected.limits.model_copy(
                    update={
                        "max_input_bytes": min(32768, selected.limits.max_input_bytes),
                        "max_output_tokens": min(
                            512, selected.limits.max_output_tokens
                        ),
                        "timeout_seconds": min(60, selected.limits.timeout_seconds),
                    }
                )
            }
        )
        ports = TrialModelPorts(
            authority,
            SqlModelUsageRepository(sessions),
            lambda tenant: cast(
                ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(sessions, tenant)
            ),
            fingerprints,
            bounded.limits,
            owner,
            timedelta(seconds=bounded.limits.timeout_seconds + 30),
            selected.provider,
        )
        return ReplyRetryModelGenerator(
            identity,
            selected.model,
            SqlReplyRetryBudget(
                retries, tenant_id, message, sequence=identity.sequence
            ),
            ports,
            lambda budget, identity: configured_codex_provider(
                bounded, identity, round_budget=budget
            ),
        )

    async def replay(message: MessageId) -> ReplyClassificationResult | None:
        if not await retries.committed(tenant_id, message):
            return None
        committed = await core.conversations.get_classification(tenant_id, message)
        if committed is None or committed.classified_by != settings.model:
            raise ModelGenerationError("permission")
        return ReplyClassificationResult(
            category=committed.category,
            candidate_fields=tuple(
                ReplyFieldCandidate(f.field, f.value, f.quote)
                for f in committed.candidate_fields
            ),
            suppress_scope=committed.suppress_scope,
        )

    classifier = BoundReplyClassifier(
        authority,
        model.generator,
        model=settings.model,
        configuration_version=settings.configuration_version,
        max_output_tokens=min(settings.limits.max_output_tokens, 512)
        if settings.qq_reply and settings.qq_reply.continuous else settings.limits.max_output_tokens,
        retry_generator=retry_generator,
        select_model=select_ordinary,
        replay=replay,
    )
    from .dialogue_admission import ColdReplyDialogueAdmission
    admission = ColdReplyDialogueAdmission(sessions, tenant_id, outreach, core.conversations, mailbox, dialogue, resources.now) if dialogue is not None else None
    reply = CurrentEmployeeReplyFactory(
        tenant_id,
        employee_id,
        classifier=classifier,
        trial_gate=trial_gate,
        retries=retries,
        dialogue_access=dialogue,
        dialogue_admission=admission,
        recovery_only=bool(settings.qq_reply and settings.qq_reply.recovery_only),
    )(core, outreach, resources)

    if dialogue is not None:
        if resources.generated_store is None:
            raise ValueError("普通对话缺少不可变计划原件端口")
        from artifact_store.store import BoundedGeneratedArtifactStore
        from infra.db.dialogue_execution import SqlDialogueExecutionStore
        from tool_gateway.handlers.dialogue_send import (
            MANIFEST,
            DialogueSendHandler,
            dialogue_checks,
        )
        from tool_gateway.manifest import ToolRegistry
        from tool_gateway.pipeline import ToolGateway

        from .dialogue_admission import ColdReplyDialogueAdmission
        from .dialogue_delivery import CurrentDialogueDelivery
        from .dialogue_driver import OrdinaryDialogueDriver
        from .dialogue_plan_authority import DialoguePlanAuthority
        from .dialogue_plan_store import GatewayDialoguePlanStore
        store = SqlDialogueExecutionStore(sessions, tenant_id, now=resources.now)
        planning = OrdinaryDialogueModels(sessions, settings, dialogue,
            DialoguePlanAuthority(sessions, tenant_id, dialogue, configuration, now=resources.now), fingerprints, owner)
        class Progress:
            async def progress(self, message: MessageId) -> tuple[str, tuple[str, ...]]:
                return await driver.progress(message)
        delivery = CurrentDialogueDelivery(sessions, tenant_id, dialogue, store,
            cast(BoundedGeneratedArtifactStore, resources.generated_store), core.sending,
            mailbox_binding, Progress(), settings=settings)
        registry = ToolRegistry()
        registry.register(MANIFEST, DialogueSendHandler(delivery, fingerprints))
        send = ToolGateway(registry, # type: ignore[arg-type]
            dialogue_checks(delivery),
            lambda tenant: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(sessions, tenant, now=resources.now)),
            lease_duration=timedelta(minutes=2), lease_owner=owner, now=resources.now, id_factory=new_id)
        from apps.scheduler_worker.dialogue_business_inbound import (
            DialogueBusinessInboundBridge,
        )
        driver = OrdinaryDialogueDriver(sessions, tenant_id, store, dialogue, core.demand,
            core.conversations, reply.content_reader, planning,
            GatewayDialoguePlanStore(sessions, resources.generated_store, fingerprints), send,
            configuration_version=settings.configuration_version, settings=settings,
            admission=admission,
            business_inbound=DialogueBusinessInboundBridge(sessions, tenant_id, mailbox,
                SqlDialogueOwnerAccess(sessions, tenant_id, mailbox), outreach, core.sending, cold_classifier=employee_id))
        reply = replace(reply, dialogue_driver=driver)
    return reply
