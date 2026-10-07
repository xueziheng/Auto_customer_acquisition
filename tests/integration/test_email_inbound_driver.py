"""沿原owned supervisor验证受控API、scheduler独立进程与同owner恢复。"""

from __future__ import annotations

# ruff: noqa: F811 - pytest复用owned fixture
import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

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
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.unit.test_email_inbound import mime


@pytest.fixture
def inbound_migration_url() -> Iterator[SecretStr]:
    """迁移仅使用本例新建测试库的管理连接，不获取演练 runtime 的管理凭证。"""
    with PostgresContainer("pgvector/pgvector:pg16") as database:
        yield SecretStr(database.get_connection_url(driver="asyncpg"))


async def _migrate(url: SecretStr, direction: str, revision: str) -> tuple[int, bool]:
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "scripts/run_alembic.py", direction, revision],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "DATABASE_URL": url.get_secret_value(),
            "PYTHON_DOTENV_DISABLED": "1",
        },
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result.returncode, b"0059 refuses destructive inbound downgrade" in result.stderr


async def test_clean_latest_migration_roundtrip(inbound_migration_url: SecretStr):
    try:
        for direction, revision in (
            ("upgrade", "head"), ("downgrade", "0058"), ("upgrade", "head"),
        ):
            code, _ = await _migrate(inbound_migration_url, direction, revision)
            assert code == 0, "独立测试库迁移往返失败"
    finally:
        code, _ = await _migrate(inbound_migration_url, "upgrade", "head")
        assert code == 0, "迁移测试库恢复 head 失败"


async def test_0059_refuses_downgrade_with_confirmed_inbound_binding(
    inbound_migration_url: SecretStr,
):
    tenant = str(new_id("tn"))
    identity = str(new_id("sid"))
    engine = create_engine_from(inbound_migration_url.get_secret_value())
    try:
        # 从目标版本验其保护逻辑，后续迁移 DDL 与运行角色权限均不参与此断言。
        code, _ = await _migrate(inbound_migration_url, "upgrade", "0059")
        assert code == 0, "准备 0059 测试库失败"
        async with engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO sending_domains (tenant_id, domain, role, created_at)
                VALUES (:tenant, 'example.invalid', 'cold_outreach', now())
            """), {"tenant": tenant})
            await connection.execute(text("""
                INSERT INTO sending_identities (
                    tenant_id, identity_id, domain, address, state, version,
                    throttle_hard_bounce_rate, suspend_hard_bounce_rate,
                    throttle_complaint_rate, suspend_complaint_rate,
                    suspend_on_spam_trap, suspend_on_blocklist, minimum_sample, created_at
                ) VALUES (
                    :tenant, :identity, 'example.invalid', 'sender@example.invalid',
                    'unconfigured', 0, 0.02, 0.05, 0.001, 0.002, true, true, 100, now()
                )
            """), {"tenant": tenant, "identity": identity})
            await connection.execute(text("""
                INSERT INTO email_inbound_cursors (
                    tenant_id, mailbox_alias, configured_identity_id, route_id,
                    config_version, provider_cursor, version, bootstrap_started_at,
                    after_epoch, confirmed_by, confirmed_at
                ) VALUES (
                    :tenant, 'primary', :identity, 'controlled', 'v1', 'synthetic-cursor',
                    1, now(), 0, :employee, now()
                )
            """), {"tenant": tenant, "identity": identity, "employee": str(new_id("emp"))})
        code, protected = await _migrate(inbound_migration_url, "downgrade", "0058")
        assert code != 0 and protected, "应由 0059 数据保护拒绝降级"
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0059"
            assert await connection.scalar(text(
                "SELECT provider_cursor FROM email_inbound_cursors WHERE tenant_id = :tenant"
            ), {"tenant": tenant}) == "synthetic-cursor"
            for table in ("email_inbound_receipts", "email_inbound_reviews"):
                assert await connection.scalar(text("SELECT to_regclass(:table)"), {"table": table}) == table
    finally:
        try:
            async with engine.begin() as connection:
                if await connection.scalar(text("SELECT to_regclass('email_inbound_cursors')")):
                    await connection.execute(text(
                        "DELETE FROM email_inbound_cursors WHERE tenant_id = :tenant"
                    ), {"tenant": tenant})
                    await connection.execute(text(
                        "DELETE FROM sending_identities WHERE tenant_id = :tenant"
                    ), {"tenant": tenant})
                    await connection.execute(text(
                        "DELETE FROM sending_domains WHERE tenant_id = :tenant"
                    ), {"tenant": tenant})
        finally:
            await engine.dispose()
            code, _ = await _migrate(inbound_migration_url, "upgrade", "head")
            assert code == 0, "迁移测试库恢复 head 失败"


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
