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
from domains.outreach.service import OutreachService
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.run_audit import PostgresRunAuditRepository
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import PILOT_GMAIL_MAILBOX_ALIAS, PilotConfig
from infra.secrets import EnvironmentSecretResolver
from infra.standalone.automatic_followup import reply_employee_binding
from infra.standalone.hunter_settings import (
    HunterCustomerSettings,
    load_hunter_customer_settings,
)
from infra.standalone.knowledge_settings import (
    KnowledgeSettings,
    load_knowledge_settings,
)
from infra.standalone.platform_settings import load_platform_settings
from infra.standalone.runtime import ModelRuntimeLifecycle
from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from shared.schemas.identifiers import TenantId, UserId, new_id
from shared.schemas.model_invocation import InvocationIdentity
from shared.schemas.money import CurrencyCode, Money
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import ModelProvider
from tool_gateway.qq_trial_model import TrialProviderBudget
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.account_discovery.ports import ContactVerifier
from workflows.assistant.ports import AssistantRuntimePorts
from workflows.engine.audit import Phase1RunAuditAuthorizer, RunAuditService

from .bootstrap import CanonicalSchedulerBootstrap, ResearchRuntimePorts
from .config import SchedulerWorkerConfig
from .customer_preparation_runtime import PreparationRuntime
from .dialogue_resources import DialogueArtifactPorts
from .inventory_ports import InventoryRuntimePorts
from .main import EnterpriseKnowledgeDriverProtocol, SchedulerRuntime
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
    if reply_employee_binding(profile, settings) is None:
        if provided is not None:
            raise ValueError("回复未启用时不能注入入站端口")
        return None
    qq = settings.qq_reply
    if qq is not None:
        from apps.composition_support.email_inbound import qq_adapter
        if qq.trial is not None:
            from .qq_trial_startup import QQTrialRuntimePorts, QQTrialStartup
            if provided is not None:
                raise ValueError("受控 QQ 必须经过原启动采集装配")
            startup = QQTrialStartup(profile,settings)
            route = qq.trial.route
            return QQTrialRuntimePorts(startup=startup,
                profile=InboundMailbox(tenant_id=route.tenant_id,mailbox_alias=route.mailbox_alias,
                    route_id=route.route_id,config_version=route.config_version),
                provider=None,secret_resolver=profile,secret_ref="QQ_INBOUND_AUTH",
                object_settings=S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment()),
                fingerprint_key_ref="PILOT_FINGERPRINT",lease_owner="standalone-qq-trial-"+profile.owner,
                adapter_factory=startup.compose, activate_adapter=startup.activate)
        mailbox = InboundMailbox(
            tenant_id=TenantId(profile.tenant_id), mailbox_alias="qq-project-reply",
            route_id="pilot", config_version="qq-private-test-v1",
        )
        if qq.continuous is not None:
            route = qq.continuous.route
            if route.tenant_id != profile.tenant_id:
                raise ValueError("持续收信不能跨企业")
            mailbox = InboundMailbox(tenant_id=route.tenant_id, mailbox_alias=route.mailbox_alias,
                route_id=route.route_id, config_version=route.config_version)
        if provided is not None:
            if provided.profile != mailbox or provided.provider is not None or provided.activate_adapter is not None or provided.scan_enabled != (not qq.recovery_only) or (provided.adapter is None) == (provided.adapter_factory is None):
                raise ValueError("QQ回复端口与明确配置不一致")
            return provided
        return InboundRuntimePorts(
            profile=mailbox, provider=None, secret_resolver=profile,
            secret_ref="QQ_INBOUND_AUTH",
            object_settings=S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment()),
            fingerprint_key_ref="PILOT_FINGERPRINT",
            lease_owner="standalone-qq-inbound-" + profile.owner,
            adapter_factory=lambda sessions: qq_adapter(qq, TenantId(profile.tenant_id), sessions),
            scan_enabled=not qq.recovery_only,
            poll_interval_seconds=qq.continuous.poll_interval_seconds if qq.continuous else 0,
        )
    gmail = profile.gmail
    assert gmail is not None
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
    trial_provider_factory: Callable[[TrialProviderBudget, InvocationIdentity], ModelProvider] | None = None,
    research_ports: ResearchRuntimePorts | None = None,
    inventory_ports: InventoryRuntimePorts | None = None,
    reply_inbound: InboundRuntimePorts | None = None,
    instance_id: str,
    knowledge_settings: KnowledgeSettings | None = None,
    inventory_control: tuple[async_sessionmaker[AsyncSession], TenantId] | None = None,
    qq_dns_enabled: bool = False,
    qq_mail_directory: Path | None = None,
    hunter_customer_settings: HunterCustomerSettings | None = None,
) -> SchedulerRuntimeFactory:
    if settings.qq_reply is not None and settings.qq_reply.trial is not None:
        if not settings.model_data_export_enabled:
            raise ValueError("受控回复必须保留显式模型数据许可")
        if trial_provider_factory is None and (settings.model_catalog_directory is None or settings.codex_job_root is None):
            raise ValueError("受控回复必须配置原租户 Codex 模型入口")
    ordinary_enabled = settings.reply_enabled and qq_mail_directory is not None and settings.qq_reply is None
    ordinary_only = ordinary_enabled and profile.gmail is None
    if ordinary_only and reply_inbound is not None:
        raise ValueError("普通询盘模式不能注入冷收信端口")
    inbound = None if ordinary_only else _reply_ports(profile, settings, reply_inbound)
    send_transport: GmailHttpTransport | None = None
    reply_employee = reply_employee_binding(profile, settings, ordinary_mailboxes=ordinary_enabled)
    if settings.automatic_send_enabled and not ordinary_only:
        if settings.qq_reply is not None or profile.gmail is None or inbound is None or not isinstance(inbound.provider, GmailHttpTransport):
            raise ValueError("自动发送必须具有独立启用与完整Gmail配置")
        send_transport = inbound.provider
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

    def knowledge_driver(sessions: async_sessionmaker[AsyncSession]) -> EnterpriseKnowledgeDriverProtocol:
        from .knowledge import build_knowledge_driver

        assert knowledge_settings is not None and knowledge_settings.enabled
        return build_knowledge_driver(
            profile=profile, settings=settings, knowledge=knowledge_settings,
            model_resolver=model_resolver, sessions=sessions,
            fingerprints=fingerprints, instance_id=instance_id,
        )

    def inventory_driver(core, sessions):
        from .standalone_inventory import build_inventory_driver
        assert inventory_control is not None and (inventory_ports is not None or research_ports is not None)
        return build_inventory_driver(core=core,sessions=sessions,control_sessions=inventory_control[0],
            control_tenant=inventory_control[1],tenant_id=tenant,settings=settings,resolver=model_resolver,
            fingerprints=fingerprints,research=research_ports,owner=instance_id, inventory_ports=inventory_ports)

    async def preparation_driver(core: SchedulerCoreServices, sessions: async_sessionmaker[AsyncSession], outreach: OutreachService, verifier: ContactVerifier | None) -> PreparationRuntime:
        from .customer_preparation_runtime import build_customer_preparation
        assert inventory_control is not None
        return await build_customer_preparation(core=core, sessions=sessions, control_sessions=inventory_control[0],
            control_tenant=inventory_control[1], tenant_id=tenant, outreach=outreach, existing_verifier=verifier,
            settings=settings, resolver=model_resolver, fingerprints=fingerprints, research=research_ports,
            owner=instance_id, hunter=hunter_customer_settings, inventory_ports=inventory_ports)

    def formal_mail(sessions):
        from connectors.qq_mail.client import QQMailConnector
        from infra.aliyun_mail_store import AliyunMailStore
        from infra.db.campaign_mail_owner import CampaignMailboxOwner
        from infra.outbound_mail import AliyunOutboundConnectors, EmployeeMailConnectors
        assert qq_mail_directory is not None
        store = AliyunMailStore(qq_mail_directory)
        return (EmployeeMailConnectors(None, None, AliyunOutboundConnectors(store, sessions,
            connector_factory=QQMailConnector)), CampaignMailboxOwner(sessions, store))

    scoring, handoff = profile.policy.scoring_policy, profile.policy.handoff_policy
    return SchedulerRuntimeFactory(
        env,
        pilot_config=SchedulerWorkerConfig.from_pilot_environ(env),
        standalone_research=research_ports is not None,
        secret_resolver=inbound.secret_resolver if inbound is not None else profile,
        inbound_ports=inbound,
        reply_artifact_ports=(DialogueArtifactPorts(S3ObjectStoreSettings.from_pilot_environ(env), profile) if ordinary_enabled else None),
        unconfigured_dns_step=UnconfiguredDnsStep(),
        qq_dns_enabled=qq_dns_enabled,
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
            inventory_factory=inventory_driver if inventory_control is not None and (inventory_ports is not None or research_ports is not None) else None,
            preparation_factory=preparation_driver if inventory_control is not None else None,
            knowledge_factory=knowledge_driver if knowledge_settings is not None and knowledge_settings.enabled else None,
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
            campaign_enabled=settings.automatic_send_enabled and (send_transport is not None or (ordinary_enabled and reply_employee is not None)),
            outbound_factory=formal_mail if ordinary_enabled and settings.automatic_send_enabled and reply_employee is not None else None,
            gmail_transport=send_transport,
            secret_resolver=inbound.secret_resolver if inbound is not None else None,
            reply_factory=(
                lambda core, outreach, resources: bind_reply(
                    core,
                    outreach,
                    resources,
                    tenant_id=tenant,
                    employee_id=reply_employee,
                    settings=settings,
                    resolver=model_resolver,
                    fingerprints=fingerprints,
                    owner=instance_id,
                    mailbox_directory=qq_mail_directory if ordinary_enabled else None,
                    provider_factory=provider_factory,
                    trial_gate=resources.trial_gate,
                    trial_provider_factory=trial_provider_factory,
                )
            )
            if reply_employee is not None or ordinary_enabled
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
    trial_provider_factory: Callable[[TrialProviderBudget, InvocationIdentity], ModelProvider] | None = None,
    research_ports: ResearchRuntimePorts | None = None,
    inventory_ports: InventoryRuntimePorts | None = None,
    reply_inbound: InboundRuntimePorts | None = None,
    qq_dns_enabled: bool = False,
    qq_mail_directory: Path | None = None,
    hunter_customer_settings_file: Path | None = None,
) -> Callable[[], AbstractAsyncContextManager[SchedulerRuntime]]:
    """构造无 IO；对象 SDK 在进入进程生命周期后拥有并对称关闭。"""

    @asynccontextmanager
    async def resources() -> AsyncIterator[SchedulerRuntime]:
        from connectors.object_store.bounded import S3BoundedObjectBlobTransport
        from connectors.object_store.config import S3ObjectStoreSettings
        from connectors.object_store.s3 import S3ObjectBlobTransport
        from connectors.tavily.transport import TavilySearchApiTransport
        from connectors.web_search.transport import SafePublicPageHttpTransport
        from shared.schemas.evidence_read import ObjectReadLimits

        knowledge_settings = (
            load_knowledge_settings(profile.knowledge_settings_file)
            if profile.knowledge_settings_file is not None else None
        )
        hunter_customer_settings = load_hunter_customer_settings(hunter_customer_settings_file)
        ports = research_ports
        page_ports = inventory_ports
        owned: S3ObjectBlobTransport | None = None
        control_engine = None
        inventory_control = None
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
                    bounded_object_transport=S3BoundedObjectBlobTransport(
                        S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment()), profile,
                        limits=ObjectReadLimits(connect_timeout_ms=3000, read_timeout_ms=5000,
                            total_timeout_ms=15000, chunk_bytes=65536, maximum_attempts=1)),
                )
            if page_ports is None and ports is not None:
                page_ports = InventoryRuntimePorts(ports.page_transport, ports.object_transport,
                    ports.maximum_artifact_bytes, ports.bounded_object_transport)
            if page_ports is None and profile.platform_settings_file is not None:
                owned = S3ObjectBlobTransport(S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment()), profile)
                page_ports = InventoryRuntimePorts(SafePublicPageHttpTransport(timeout_seconds=10), owned, 4 * 1024 * 1024,
                    S3BoundedObjectBlobTransport(S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment()), profile,
                        limits=ObjectReadLimits(connect_timeout_ms=3000, read_timeout_ms=5000,
                            total_timeout_ms=15000, chunk_bytes=65536, maximum_attempts=1)))
            if profile.platform_settings_file is not None:
                platform = load_platform_settings(profile.platform_settings_file).model_copy(update={"database_port":profile.database_port})
                if platform.control_tenant_id == profile.tenant_id:
                    raise ValueError('库存控制租户必须独立')
                control_engine = create_engine_from(platform.database_url.get_secret_value())
                await assert_database_schema_current(control_engine)
                inventory_control = (async_sessionmaker(control_engine,expire_on_commit=False),TenantId(platform.control_tenant_id))
            factory = _create_standalone_factory(
                profile,
                settings,
                model_resolver,
                provider_factory=provider_factory,
                trial_provider_factory=trial_provider_factory,
                research_ports=ports,
                inventory_ports=page_ports,
                reply_inbound=reply_inbound,
                instance_id=new_id("mrt").lower(),
                knowledge_settings=knowledge_settings,
                inventory_control=inventory_control,
                qq_dns_enabled=qq_dns_enabled,
                qq_mail_directory=qq_mail_directory,
                hunter_customer_settings=hunter_customer_settings,
            )
            async with factory() as runtime:
                yield runtime
        finally:
            if control_engine is not None:
                await control_engine.dispose()
            if owned is not None:
                await owned.aclose()

    return resources


def main() -> int:
    parser = argparse.ArgumentParser(description="启动独立本机 TradeOS 后台")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--model-settings", type=Path, required=True)
    parser.add_argument("--hunter-customer-settings", type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        profile = PilotConfig.read(args.profile)
        settings = load_model_settings(args.model_settings)
        return run_worker(
            create_standalone_factory(
                profile, settings, EnvironmentSecretResolver(os.environ),
                qq_dns_enabled=(args.profile.absolute().parent / "qq-mail").is_dir(),
                qq_mail_directory=(args.profile.absolute().parent / "qq-mail" if (args.profile.absolute().parent / "qq-mail").is_dir() else None),
                hunter_customer_settings_file=(args.hunter_customer_settings or
                    ((args.profile.absolute().parent / "hunter-customer-settings.json")
                     if (args.profile.absolute().parent / "hunter-customer-settings.json").is_file() else None)),
            )
        )
    except BaseException:  # noqa: BLE001 - 进程边界只返回固定安全失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
