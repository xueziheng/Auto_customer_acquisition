"""独立本机 scheduler；真实模型仅在持锁后台的显式任务内访问。"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.assistant.model_authority import AssistantModelAuthority
from agent_runtime.assistant.proposal import ResearchProposalBuilder
from apps.composition_support.assistant import build_assistant_core
from apps.composition_support.email_inbound import InboundMailbox, InboundRuntimePorts
from apps.composition_support.model import build_model_composition
from connectors.gmail.client import SecretResolver
from connectors.gmail.inbound_transport import GmailInboundApiTransport
from connectors.gmail.send_oauth import GmailOAuthSecretResolver, GmailOAuthTokenSource
from connectors.gmail.transport import GmailHttpTransport
from connectors.object_store.config import S3ObjectStoreSettings
from domains.employees.permissions import Actor, EmployeeScope
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service import OpportunityService
from domains.opportunities.service_impl import HandoffPolicy
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.run_audit import PostgresRunAuditRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import PILOT_GMAIL_MAILBOX_ALIAS, PilotConfig
from infra.secrets import EnvironmentSecretResolver
from infra.standalone.runtime import ModelRuntimeLifecycle
from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id
from shared.schemas.money import CurrencyCode, Money
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import ModelProvider
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.assistant.ports import AssistantRuntimePorts
from workflows.engine.audit import Phase1RunAuditAuthorizer, RunAuditService

from .bootstrap import CanonicalSchedulerBootstrap, ResearchRuntimePorts
from .config import SchedulerWorkerConfig
from .main import SchedulerRuntime
from .main import main as run_worker
from .pilot import UnconfiguredDnsResolver, UnconfiguredDnsStep
from .runtime import (
    SchedulerCoreServices,
    SchedulerHealthServer,
    SchedulerRuntimeFactory,
)
from .standalone_reply import bind_reply
from .standalone_research import bind_research


def _reply_ports(
    profile: PilotConfig,
    settings: StandaloneModelSettings,
    provided: InboundRuntimePorts | None,
) -> InboundRuntimePorts | None:
    """沿用原 Gmail 路由；端口注入不能跨租户、换邮箱或重置版本。"""
    if not settings.reply_enabled:
        if provided is not None:
            raise ValueError("回复未启用时不能注入入站端口")
        return None
    gmail = profile.gmail
    if gmail is None or not gmail.employee_id or not gmail.employee_id.strip():
        raise ValueError("回复模型缺少明确 Gmail 与员工绑定")
    mailbox = InboundMailbox(
        tenant_id=TenantId(profile.tenant_id),
        mailbox_alias=PILOT_GMAIL_MAILBOX_ALIAS,
        route_id="pilot",
        config_version="pilot-gmail-v1",
    )
    if provided is not None:
        if (
            provided.profile != mailbox
            or not isinstance(provided.provider, GmailHttpTransport)
            or not isinstance(provided.secret_resolver, SecretResolver)
            or not all(
                callable(getattr(provided.provider, method, None))
                for method in (
                    "get_profile_history_id",
                    "list_feedback_messages",
                    "list_feedback_history",
                    "get_inbound_message",
                )
            )
        ):
            raise ValueError("回复入站端口与部署绑定不一致")
        return provided
    return InboundRuntimePorts(
        profile=mailbox,
        provider=GmailInboundApiTransport(),
        secret_resolver=GmailOAuthSecretResolver(
            profile, GmailOAuthTokenSource(gmail.credentials_file, gmail.address)
        ),
        secret_ref="GMAIL_OAUTH_TOKEN_REF",
        object_settings=S3ObjectStoreSettings.from_pilot_environ(
            profile.runtime_environment()
        ),
        fingerprint_key_ref="PILOT_FINGERPRINT",
        lease_owner="standalone-scheduler-inbound-" + profile.owner,
    )


def _create_standalone_factory(
    profile: PilotConfig,
    settings: StandaloneModelSettings,
    model_resolver: SecretResolver,
    *,
    provider_factory: Callable[[], ModelProvider] | None = None,
    research_ports: ResearchRuntimePorts | None = None,
    reply_inbound: InboundRuntimePorts | None = None,
    instance_id: str,
) -> SchedulerRuntimeFactory:
    inbound = _reply_ports(profile, settings, reply_inbound)
    env = profile.runtime_environment()
    tenant = TenantId(profile.tenant_id)
    fingerprints = HmacFingerprintProvider(
        env["TOOL_CALL_FINGERPRINT_KEY_VERSION"],
        profile.resolve(env["TOOL_CALL_FINGERPRINT_KEY_REF"]).encode(),
    )

    def assistant(
        core: SchedulerCoreServices,
        sessions: async_sessionmaker[AsyncSession],
        opportunities: OpportunityService,
    ) -> AssistantRuntimePorts:
        now = lambda: datetime.now(UTC)
        shared = build_assistant_core(
            factory=sessions,
            settings=settings,
            employees=core.employee_scope,
            lookup=Actor("system:assistant", EmployeeScope.SYSTEM, "system"),
            opportunities=opportunities,
            demand=core.demand,
            run_audit=RunAuditService(
                PostgresRunAuditRepository(sessions), Phase1RunAuditAuthorizer(tenant)
            ),
            fingerprints=fingerprints,
            now=now,
        )
        usage = SqlModelUsageRepository(sessions)
        lifecycle = ModelRuntimeLifecycle(
            shared.repository,
            settings,
            tenant,
            "scheduler",
            recovery=lambda owner: usage.recover_abandoned(tenant, owner, now()),
            instance_id=instance_id,
        )
        model = build_model_composition(
            settings=settings,
            resolver=model_resolver,
            authority=AssistantModelAuthority(shared.configuration, shared.service),
            usage=usage,
            ledger_factory=lambda tenant_id: cast(
                ToolGatewayUnitOfWork,
                SqlAlchemyToolGatewayUnitOfWork(sessions, tenant_id),
            ),
            fingerprints=fingerprints,
            lease_owner=lifecycle.instance_id,
            lease_duration=timedelta(seconds=settings.limits.timeout_seconds + 30),
            provider_factory=provider_factory,
        )
        return AssistantRuntimePorts(
            shared.service,
            shared.context,
            shared.reads,
            model.generator,
            ResearchProposalBuilder(),
            core.directives,
            shared.identity,
            fingerprints,
            settings.model,
            settings.configuration_version,
            settings.limits.max_output_tokens,
            shared.configuration,
            lifecycle,
        )

    scoring, handoff = profile.policy.scoring_policy, profile.policy.handoff_policy
    return SchedulerRuntimeFactory(
        env,
        pilot_config=SchedulerWorkerConfig.from_pilot_environ(env),
        standalone_research=research_ports is not None,
        secret_resolver=inbound.secret_resolver if inbound is not None else profile,
        inbound_ports=inbound,
        unconfigured_dns_step=UnconfiguredDnsStep(),
        bootstrap=CanonicalSchedulerBootstrap(
            ScoringPolicy(
                scoring.version,
                tuple(
                    Money(v, CurrencyCode(scoring.currency))
                    for v in scoring.value_band_boundaries
                ),
                {int(k): v for k, v in scoring.bucket_map.items()},
            ),
            HandoffPolicy(handoff.sla_seconds, handoff.backlog_threshold),
            assistant_factory=assistant,
            research_enabled=research_ports is not None,
            research_factory=(
                lambda core, sessions: bind_research(
                    core,
                    sessions,
                    settings,
                    model_resolver,
                    fingerprints,
                    instance_id,
                    research_ports,
                    provider_factory,
                )
            )
            if research_ports is not None
            else None,
            campaign_enabled=inbound is not None,
            gmail_transport=inbound.provider if inbound is not None else None,
            secret_resolver=inbound.secret_resolver if inbound is not None else None,
            reply_factory=(
                lambda core, outreach, resources: bind_reply(
                    core,
                    outreach,
                    resources,
                    tenant_id=tenant,
                    employee_id=EmployeeId(profile.gmail.employee_id),
                    settings=settings,
                    resolver=model_resolver,
                    fingerprints=fingerprints,
                    owner=instance_id,
                    provider_factory=provider_factory,
                )
            )
            if inbound is not None
            else None,
        ),
        resolver_factory=UnconfiguredDnsResolver,
        health_server_factory=lambda state, port: SchedulerHealthServer(
            state, port, host="127.0.0.1"
        ),
    )


class UnboundResearchClient:
    async def complete_json(
        self, *, model: str, system_prompt: str, payload: object, max_output_tokens: int
    ) -> str:
        raise ValueError("研究模型必须绑定已确认 Run")


def create_standalone_factory(
    profile: PilotConfig,
    settings: StandaloneModelSettings,
    model_resolver: SecretResolver,
    *,
    provider_factory: Callable[[], ModelProvider] | None = None,
    research_ports: ResearchRuntimePorts | None = None,
    reply_inbound: InboundRuntimePorts | None = None,
) -> Callable[[], AbstractAsyncContextManager[SchedulerRuntime]]:
    """构造无 IO；对象 SDK 在进入进程生命周期后拥有并对称关闭。"""

    @asynccontextmanager
    async def resources() -> AsyncIterator[SchedulerRuntime]:
        from connectors.object_store.config import S3ObjectStoreSettings
        from connectors.object_store.s3 import S3ObjectBlobTransport
        from connectors.tavily.transport import TavilySearchApiTransport
        from connectors.web_search.transport import SafePublicPageHttpTransport

        ports = research_ports
        owned: S3ObjectBlobTransport | None = None
        research = settings.research
        if ports is not None and research is None:
            raise ValueError("研究端口缺少显式部署配置")
        try:
            if research is not None and ports is None:
                owned = S3ObjectBlobTransport(
                    S3ObjectStoreSettings.from_pilot_environ(
                        profile.runtime_environment()
                    ),
                    profile,
                )
                ports = ResearchRuntimePorts(
                    UnboundResearchClient(),
                    settings.model,
                    UserId(research.playbook_reader_user_id),
                    TavilySearchApiTransport(
                        timeout_seconds=research.search_timeout_seconds
                    ),
                    SafePublicPageHttpTransport(
                        timeout_seconds=research.page_timeout_seconds
                    ),
                    owned,
                    research.maximum_artifact_bytes,
                    research.secret_ref,
                    model_resolver,
                    research.exclusive_account_confirmed,
                )
            factory = _create_standalone_factory(
                profile,
                settings,
                model_resolver,
                provider_factory=provider_factory,
                research_ports=ports,
                reply_inbound=reply_inbound,
                instance_id=new_id("mrt").lower(),
            )
            async with factory() as runtime:
                yield runtime
        finally:
            if owned is not None:
                await owned.aclose()

    return resources


def main() -> int:
    parser = argparse.ArgumentParser(description="启动独立本机 TradeOS 后台")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--model-settings", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        profile = PilotConfig.read(args.profile)
        settings = load_model_settings(args.model_settings)
        return run_worker(
            create_standalone_factory(
                profile, settings, EnvironmentSecretResolver(os.environ)
            )
        )
    except BaseException:  # noqa: BLE001 - 进程边界只返回固定安全失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
