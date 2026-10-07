"""独立入口的回复模型接线；资源和生命周期归原 scheduler 所有。"""

from collections.abc import Callable
from datetime import timedelta
from typing import cast

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from apps.composition_support.model import build_model_composition
from connectors.gmail.client import SecretResolver
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import Actor, EmployeeScope
from domains.outreach.service import OutreachService
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.reply_model_binding import SqlReplyRunBindingReader
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import EmployeeId, TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import ModelProvider
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
) -> ReplyQualificationComposition:
    """只借用原进程的 SQL/原件/域端口，不启动第二套循环或模型心跳。"""
    if not settings.reply_enabled:
        raise ValueError("回复模型必须显式启用")
    sessions = resources.sessions
    lookup = Actor("system:reply-model", EmployeeScope.SYSTEM, "system")
    current = CurrentEmployeeIdentity(core.employee_scope, lookup)
    configuration = ModelConfigurationServiceImpl(
        SqlModelConfigurationRepository(sessions, fingerprints, now=resources.now),
        current,
        now=resources.now,
    )
    authority = ReplyModelAuthority(
        SqlReplyRunBindingReader(sessions),
        core.employee_scope,
        lookup,
        configuration,
        tenant_id=tenant_id,
        employee_id=employee_id,
        model=settings.model,
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
        provider_factory=provider_factory,
    )
    classifier = BoundReplyClassifier(
        authority,
        model.generator,
        model=settings.model,
        configuration_version=settings.configuration_version,
        max_output_tokens=settings.limits.max_output_tokens,
    )
    return CurrentEmployeeReplyFactory(tenant_id, employee_id, classifier=classifier)(
        core, outreach, resources
    )
