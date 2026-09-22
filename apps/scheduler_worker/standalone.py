"""独立本机 scheduler；真实模型仅在持锁后台的显式任务内访问。"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.assistant.model_authority import AssistantModelAuthority
from agent_runtime.assistant.proposal import ResearchProposalBuilder
from apps.composition_support.assistant import build_assistant_core
from apps.composition_support.model import build_model_composition
from connectors.gmail.client import SecretResolver
from domains.employees.permissions import Actor, EmployeeScope
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service import OpportunityService
from domains.opportunities.service_impl import HandoffPolicy
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.run_audit import PostgresRunAuditRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import PilotConfig
from infra.secrets import EnvironmentSecretResolver
from infra.standalone.runtime import ModelRuntimeLifecycle
from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from shared.schemas.identifiers import TenantId
from shared.schemas.money import CurrencyCode, Money
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import ModelProvider
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.assistant.ports import AssistantRuntimePorts
from workflows.engine.audit import Phase1RunAuditAuthorizer, RunAuditService

from .bootstrap import CanonicalSchedulerBootstrap
from .config import SchedulerWorkerConfig
from .main import main as run_worker
from .pilot import UnconfiguredDnsResolver, UnconfiguredDnsStep
from .runtime import (
    SchedulerCoreServices,
    SchedulerHealthServer,
    SchedulerRuntimeFactory,
)


def create_standalone_factory(
    profile: PilotConfig,
    settings: StandaloneModelSettings,
    model_resolver: SecretResolver,
    *,
    provider_factory: Callable[[], ModelProvider] | None = None,
) -> SchedulerRuntimeFactory:
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
            shared.repository, settings, tenant, "scheduler",
            recovery=lambda owner: usage.recover_abandoned(tenant, owner, now()),
        )
        model = build_model_composition(
            settings=settings,
            resolver=model_resolver,
            authority=AssistantModelAuthority(shared.configuration, shared.service),
            usage=usage,
            ledger_factory=lambda tenant_id: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(
                sessions, tenant_id
            )),
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
        secret_resolver=profile,
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
        ),
        resolver_factory=UnconfiguredDnsResolver,
        health_server_factory=lambda state, port: SchedulerHealthServer(
            state, port, host="127.0.0.1"
        ),
    )


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
