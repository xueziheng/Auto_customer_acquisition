"""本机 scheduler：复用 canonical 流程、单实例锁和显式业务政策。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from infra.controlled.network import install_network_boundary
from infra.pilot.config import PilotConfig, PilotError
from shared.schemas.money import CurrencyCode, Money
from workflows.engine.runner import WorkflowRun

from .bootstrap import CanonicalSchedulerBootstrap
from .config import SchedulerWorkerConfig
from .main import main as run_worker
from .runtime import SchedulerHealthServer, SchedulerRuntimeFactory


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
    return SchedulerRuntimeFactory(
        env,
        pilot_config=SchedulerWorkerConfig.from_pilot_environ(env),
        secret_resolver=config,
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
            research_enabled=False,
            contacts_enabled=False,
            campaign_enabled=False,
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
        install_network_boundary(
            destinations=frozenset({config.database_port}),
            listeners=frozenset({config.scheduler_port}),
        )
        return run_worker(create_pilot_factory(config))
    except BaseException:  # noqa: BLE001 进程边界固定失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
