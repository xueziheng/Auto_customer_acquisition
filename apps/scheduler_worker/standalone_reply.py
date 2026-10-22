"""独立入口的回复模型接线；资源和生命周期归原 scheduler 所有。"""

from collections.abc import Callable
from datetime import timedelta
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
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId
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
    employee_id: EmployeeId,
    settings: StandaloneModelSettings,
    resolver: SecretResolver,
    fingerprints: HmacFingerprintProvider,
    owner: str,
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
    authority = ReplyModelAuthority(
        SqlReplyRunBindingReader(sessions),
        core.employee_scope,
        lookup,
        configuration,
        tenant_id=tenant_id,
        employee_id=employee_id,
        model=settings.model,
        retries=retries,
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
        replay=replay,
    )
    return CurrentEmployeeReplyFactory(
        tenant_id,
        employee_id,
        classifier=classifier,
        trial_gate=trial_gate,
        retries=retries,
        recovery_only=bool(settings.qq_reply and settings.qq_reply.recovery_only),
    )(core, outreach, resources)
