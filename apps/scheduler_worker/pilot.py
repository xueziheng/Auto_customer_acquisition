"""本机 scheduler：复用 canonical 流程、单实例锁和显式业务政策。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from agent_runtime.qualification_agent.deterministic import (
    DeterministicReplyClassifier,
)
from apps.composition_support.email_inbound import InboundMailbox, InboundRuntimePorts
from connectors.gmail.inbound_transport import GmailInboundApiTransport
from connectors.gmail.send_oauth import GmailOAuthSecretResolver, GmailOAuthTokenSource
from connectors.object_store.config import S3ObjectStoreSettings
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from infra.controlled.network import (
    install_network_boundary,
    resolve_external_destinations,
)
from infra.pilot.config import PILOT_GMAIL_MAILBOX_ALIAS, PilotConfig, PilotError
from shared.schemas.identifiers import EmployeeId, TenantId
from shared.schemas.money import CurrencyCode, Money
from workflows.engine.runner import WorkflowRun

from .bootstrap import CanonicalSchedulerBootstrap
from .config import SchedulerWorkerConfig
from .main import main as run_worker
from .reply_composition import CurrentEmployeeReplyFactory
from .runtime import SchedulerHealthServer, SchedulerRuntimeFactory

_GMAIL_HOSTS = frozenset(
    {("gmail.googleapis.com", 443), ("oauth2.googleapis.com", 443)}
)


class UnconfiguredDnsResolver:
    """本机无 DNS Provider，拒绝解析而不返回有效认证事实。"""

    async def resolve(self, name: str, rdtype: str) -> tuple[object, ...]:
        raise PilotError("dns_not_configured")


class UnconfiguredDnsStep:
    """缺少 DKIM 配置时流程明确失败，禁止拼接任意 selector。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        return "fail", "dns_not_configured", {}


def create_pilot_factory(config: PilotConfig) -> SchedulerRuntimeFactory:
    """只消费本 profile；技术密钥直接经可信 resolver，不枚举进环境。"""
    scoring = config.policy.scoring_policy
    handoff = config.policy.handoff_policy
    env = config.runtime_environment()
    gmail = config.gmail
    transport = GmailInboundApiTransport() if gmail is not None else None
    resolver = (
        GmailOAuthSecretResolver(
            config, GmailOAuthTokenSource(gmail.credentials_file, gmail.address)
        )
        if gmail is not None
        else config
    )
    inbound = (
        InboundRuntimePorts(
            profile=InboundMailbox(
                tenant_id=TenantId(config.tenant_id),
                mailbox_alias=PILOT_GMAIL_MAILBOX_ALIAS,
                route_id="pilot",
                config_version="pilot-gmail-v1",
            ),
            provider=transport,
            secret_resolver=resolver,
            secret_ref="GMAIL_OAUTH_TOKEN_REF",
            object_settings=S3ObjectStoreSettings.from_pilot_environ(env),
            fingerprint_key_ref="PILOT_FINGERPRINT",
            lease_owner="pilot-scheduler-inbound-" + config.owner,
        )
        if gmail is not None and transport is not None
        else None
    )
    return SchedulerRuntimeFactory(
        env,
        pilot_config=SchedulerWorkerConfig.from_pilot_environ(env),
        secret_resolver=resolver,
        unconfigured_dns_step=UnconfiguredDnsStep(),
        inbound_ports=inbound,
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
            research_enabled=False,
            contacts_enabled=False,
            campaign_enabled=gmail is not None,
            gmail_transport=transport,
            secret_resolver=resolver,
            reply_factory=(
                CurrentEmployeeReplyFactory(
                    TenantId(config.tenant_id),
                    EmployeeId(gmail.employee_id),
                    classifier=DeterministicReplyClassifier(),
                )
                if gmail is not None
                else None
            ),
        ),
        resolver_factory=UnconfiguredDnsResolver,
        health_server_factory=lambda state, port: SchedulerHealthServer(
            state, port, host="127.0.0.1"
        ),
    )


def main() -> int:
    """独立入口，沿用原循环/单实例/停止协议。"""
    logging.disable(logging.CRITICAL)
    try:
        config = PilotConfig.read(Path(sys.argv[1]))
        external_hosts = _GMAIL_HOSTS if config.gmail is not None else frozenset()
        install_network_boundary(
            destinations=frozenset({config.database_port, config.object_port}),
            listeners=frozenset({config.scheduler_port}),
            external_hosts=external_hosts,
            external_destinations=resolve_external_destinations(external_hosts),
        )
        return run_worker(create_pilot_factory(config))
    except BaseException:  # noqa: BLE001 进程边界固定失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
