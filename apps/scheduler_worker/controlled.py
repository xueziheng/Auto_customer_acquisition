"""受控scheduler独立bootstrap；复用唯一core和原锁/周期/信号。"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from apps.composition_support.email_inbound import InboundMailbox, InboundRuntimePorts
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from infra.controlled.config import ControlledConfig
from infra.controlled.network import install_network_boundary
from infra.controlled.providers import ControlledDnsResolver, ControlledGmailTransport
from infra.controlled.reply_model import ControlledReplyModelClient
from infra.controlled.research import (
    ControlledResearchModel,
    ControlledResearchPages,
    ControlledResearchSearch,
)
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from shared.schemas.money import CurrencyCode, Money

from .bootstrap import CanonicalSchedulerBootstrap, ResearchRuntimePorts
from .controlled_contacts import ControlledContactFactory
from .main import SchedulerRuntime
from .main import main as run_worker
from .reply_composition import CurrentEmployeeReplyFactory
from .runtime import SchedulerHealthServer, SchedulerRuntimeFactory


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        path = Path(sys.argv[1])
        config = ControlledConfig.read(path)
        install_network_boundary(
            destinations=frozenset({config.database_port, config.object_port}),
            listeners=frozenset({config.scheduler_port}),
        )
        objects = DeferredS3ObjectBlobTransport(
            S3ObjectStoreSettings.from_environ(config.runtime_environment()),
            config,
        )
        bootstrap = CanonicalSchedulerBootstrap(
            ScoringPolicy(
                "controlled-v1",
                (Money(amount=Decimal("1000.00"), currency=CurrencyCode("USD")),),
                {k: "low" for k in range(1, 8)},
            ),
            HandoffPolicy(sla_seconds=3600, backlog_threshold=10),
            research_enabled=True,
            research=ResearchRuntimePorts(
                model_client=ControlledResearchModel(),
                model="controlled-research-v1",
                user_id=UserId(config.identities[0].user_id),
                search_transport=ControlledResearchSearch(),
                page_transport=ControlledResearchPages(),
                object_transport=objects,
                maximum_artifact_bytes=10485760,
                secret_ref="CONTROLLED_RESEARCH",
                secret_resolver=config,
                exclusive_account_confirmed=True,
            ),
            contacts_enabled=True,
            contacts_factory=ControlledContactFactory(path.parent / "mail.sqlite"),
            campaign_enabled=True,
            gmail_transport=ControlledGmailTransport(
                path.parent / "mail.sqlite", tenant_id=config.tenant_id
            ),
            secret_resolver=config,
            reply_factory=CurrentEmployeeReplyFactory(
                TenantId(config.tenant_id),
                EmployeeId(config.identities[0].employee_id),
                ControlledReplyModelClient(
                    path.parent / "reply-model.sqlite", tenant_id=config.tenant_id
                ),
                "controlled-reply-v1",
            ),
        )
        factory = SchedulerRuntimeFactory(
            config.runtime_environment(),
            bootstrap=bootstrap,
            inbound_ports=InboundRuntimePorts(
                profile=InboundMailbox(
                    tenant_id=TenantId(config.tenant_id),
                    mailbox_alias="primary",
                    route_id="controlled",
                    config_version="v1",
                ),
                provider=ControlledGmailTransport(
                    path.parent / "mail.sqlite", tenant_id=config.tenant_id
                ),
                secret_resolver=config,
                secret_ref="CONTROLLED_GMAIL",
                object_settings=S3ObjectStoreSettings.from_environ(
                    config.runtime_environment()
                ),
                fingerprint_key_ref="CONTROLLED_FINGERPRINT",
                lease_owner="controlled-scheduler-inbound",
            ),
            resolver_factory=ControlledDnsResolver,
            health_server_factory=lambda state, port: SchedulerHealthServer(
                state, port, host="127.0.0.1"
            ),
        )

        @asynccontextmanager
        async def owned_runtime() -> AsyncIterator[SchedulerRuntime]:
            try:
                async with factory() as runtime:
                    yield runtime
            finally:
                await objects.aclose()

        return run_worker(owned_runtime)
    except BaseException:  # noqa: BLE001 进程边界固定失败，不能回显底层异常
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
