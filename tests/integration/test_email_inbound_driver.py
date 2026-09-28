"""沿原owned supervisor验证受控API、scheduler独立进程与同owner恢复。"""

from __future__ import annotations

# ruff: noqa: F811 - pytest复用owned fixture
import asyncio
import sys
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import build_phase1_dependencies
from apps.api.runtime_config import Phase1RuntimeSettings
from domains.sending_identity.schemas import DomainRole
from domains.sending_identity.service import (
    Actor,
    IdentityRegisterRequest,
    ScopeLevel,
    SendingIdentityScope,
)
from infra.controlled.providers import ControlledGmailTransport
from infra.db.session import create_engine_from
from shared.schemas.identifiers import TenantId
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.unit.test_email_inbound import mime


def test_clean_latest_migration_roundtrip(owned_infrastructure):
    s = owned_infrastructure
    environment = {
        **s.environ,
        "DATABASE_URL": s.config.database_url.get_secret_value(),
    }
    for direction, revision in (("downgrade", "0058"), ("upgrade", "head")):
        s.run_once(
            "inbound-migration-" + direction,
            [sys.executable, "-m", "alembic", direction, revision],
            environment,
        )


async def test_owned_api_scheduler_enabled_binding_restart(
    owned_infrastructure,
    monkeypatch,
):
    s = owned_infrastructure
    monkeypatch.syspath_prepend(str(s.root / "scripts"))  # 原CLI入口的脚本路径
    config = s.config
    tenant = TenantId(config.tenant_id)
    engine = create_engine_from(config.database_url.get_secret_value())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider = ControlledGmailTransport(s.directory / "mail.sqlite", tenant_id=tenant)
    deps = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(config.runtime_environment()),
        factory,
        now=lambda: datetime.now(UTC),
        secret_resolver=config,
        gmail_transport=provider,
    )
    boss = next(i for i in config.identities if i.role == "boss")
    sales = next(i for i in config.identities if i.role == "sales")
    try:
        sid = await deps.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                "sender@tradeos-controlled.test",
                "tradeos-controlled.test",
                DomainRole.COLD_OUTREACH,
            ),
            actor=Actor(
                boss.employee_id, SendingIdentityScope(level=ScopeLevel.TENANT), "boss"
            ),
        )
        await asyncio.to_thread(s.start_apps)
        headers = {"X-Tenant-Id": tenant, "X-Employee-Id": boss.employee_id}
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{config.api_port}",
            headers=headers,
            trust_env=False,
        ) as client:
            capabilities = (
                await client.get(
                    f"http://127.0.0.1:{config.scheduler_port}/health/capabilities"
                )
            ).json()
            assert next(c for c in capabilities if c["name"] == "inbound_body") == {
                "name": "inbound_body",
                "status": "enabled",
                "reason": "composed",
            }
            response = await client.get("/email-inbound/status")
            assert (
                response.status_code == 200 and response.json()["state"] == "disabled"
            )
            assert (
                await client.post(
                    "/email-inbound/binding",
                    json={"identity_id": sid, "cursor": "arbitrary"},
                )
            ).status_code == 400
            assert (
                await client.post(
                    "/email-inbound/binding",
                    json={"identity_id": sid},
                    headers={**headers, "X-Employee-Id": sales.employee_id},
                )
            ).status_code == 403
            response = await client.post(
                "/email-inbound/binding", json={"identity_id": sid}
            )
            assert response.status_code == 200 and response.json()["identity_id"] == sid
            raw = mime(message_id="<driver-review@example.test>")
            await provider.receive_inbound(raw, internal_date=datetime.now(UTC))
            # 当前原入口有完整reply消费者；无法匹配出站上下文的邮件必须进入待核对。
            deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < deadline:
                response = await client.get("/email-inbound/reviews")
                reviews = response.json()
                if response.status_code == 200 and len(reviews) == 1:
                    break
                await asyncio.sleep(0.1)
            assert response.status_code == 200 and len(reviews) == 1
            assert any(call.operation.startswith("inbound_") for call in await provider.list_calls())
            async with factory() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM email_inbound_receipts WHERE tenant_id=:t"
                        ),
                        {"t": tenant},
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT version FROM email_inbound_cursors WHERE tenant_id=:t AND mailbox_alias='primary'"
                        ),
                        {"t": tenant},
                    )
                    >= 2
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND event_type='InboundMessageStored'"
                        ),
                        {"t": tenant},
                    )
                    == 0
                )
            before = (await client.get("/email-inbound/status")).json()
            async with factory() as session:
                first = (
                    await session.execute(
                        text(
                            "SELECT confirmed_by,confirmed_at,bootstrap_started_at,after_epoch FROM email_inbound_cursors WHERE tenant_id=:t AND mailbox_alias='primary'"
                        ),
                        {"t": tenant},
                    )
                ).one()
            from infra.controlled.config import ControlledError

            with pytest.raises(
                ControlledError, match="inbound-protected-downgrade_failed"
            ):
                await asyncio.to_thread(
                    s.run_once,
                    "inbound-protected-downgrade",
                    [sys.executable, "-m", "alembic", "downgrade", "0058"],
                    {
                        **s.environ,
                        "DATABASE_URL": config.database_url.get_secret_value(),
                    },
                )
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    == "0067"
                )
            await asyncio.to_thread(s.restart)
            after = (await client.get("/email-inbound/status")).json()
            assert (
                after["identity_id"] == sid
                and after["version"] >= before["version"] >= 2
            )
            assert (await client.get("/email-inbound/reviews")).json() == reviews
            async with factory() as session:
                assert (
                    await session.execute(
                        text(
                            "SELECT confirmed_by,confirmed_at,bootstrap_started_at,after_epoch FROM email_inbound_cursors WHERE tenant_id=:t AND mailbox_alias='primary'"
                        ),
                        {"t": tenant},
                    )
                ).one() == first
    finally:
        await asyncio.to_thread(s.stop_apps)
        if deps.model_lifecycle:
            await deps.model_lifecycle.aclose()
        await engine.dispose()
