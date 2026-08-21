"""Phase 1 API runtime 的真实迁移、装配与资源生命周期。"""

from __future__ import annotations

import importlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from apps.api.dependencies import get_api_dependencies
from domains.employees.permissions import (
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from infra.db.outbox import PostgresEventBus
from infra.db.session import create_engine_from
from infra.db.tables import WorkflowRunRow
from shared.events.catalog import HandoffRequested
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    TenantId,
    new_id,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def _runtime_module():
    return importlib.import_module("apps.api.runtime")


def _composition_module():
    return importlib.import_module("apps.api.composition.runtime")


def _run_alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(
        ["alembic", *args],
        cwd=_REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, "alembic 命令失败（输出与连接信息已隐藏）"


def _local_unique_head() -> str:
    config = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    return heads[0]


def _runtime_env(database_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": "tenant-runtime-integration",
        "TRADEOS_DEV_MODE": "true",
        "TRADEOS_CORS_ALLOWED_ORIGINS": '["http://127.0.0.1:4173"]',
        "TRADEOS_API_RETRY_AFTER_SECONDS": "30",
        "TRADEOS_HANDOFF_POLICY": (
            '{"sla_seconds":300,"backlog_threshold":20,'
            '"t1_seconds":120,"t2_seconds":180}'
        ),
        "TRADEOS_SCORING_POLICY": (
            '{"version":"phase1-v1","currency":"USD",'
            '"value_band_boundaries":["1000","5000"],'
            '"bucket_map":{"1":"low","2":"low","3":"mid",'
            '"4":"mid","5":"high","6":"high","7":"high"}}'
        ),
        "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
        "GMAIL_OAUTH_TOKEN_REF": "gmail-oauth-phase1",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TOOL_FINGERPRINT_KEY": "f" * 32,
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
            '{"2026-v1":"UNSUBSCRIBE_HMAC_2026"}'
        ),
        "UNSUBSCRIBE_HMAC_2026": "u" * 32,
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
    }


def _request_for(app) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "app": app,
        }
    )


async def test_runtime_accepts_exact_head_and_rejects_downgraded_schema(
    db_url: str,
) -> None:
    module = _runtime_module()
    engine = create_engine_from(str(db_url))
    try:
        await module.assert_database_schema_current(engine)
        _run_alembic(str(db_url), "downgrade", "-1")
        with pytest.raises(module.RuntimeStartupError, match="API runtime 启动检查失败"):
            await module.assert_database_schema_current(engine)
    finally:
        _run_alembic(str(db_url), "upgrade", "head")
        await engine.dispose()


@pytest.mark.parametrize("include_local_head", [False, True])
async def test_runtime_rejects_unknown_or_multiple_database_heads(
    db_url: str,
    include_local_head: bool,
) -> None:
    module = _runtime_module()
    engine = create_engine_from(str(db_url))
    local_head = _local_unique_head()
    database_heads = (
        (local_head, "unknown-revision")
        if include_local_head
        else ("unknown-revision",)
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            for head in database_heads:
                await connection.execute(
                    text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
                    {"head": head},
                )
        with pytest.raises(module.RuntimeStartupError, match="API runtime 启动检查失败"):
            await module.assert_database_schema_current(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(
                text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
                {"head": local_head},
            )
        await engine.dispose()


async def test_database_readiness_returns_false_for_unavailable_database() -> None:
    module = _runtime_module()
    engine = create_engine_from("postgresql+asyncpg://127.0.0.1:1/postgres")
    try:
        assert await module.DatabaseReadinessProbe(engine).is_ready() is False
    finally:
        await engine.dispose()


async def test_runtime_lifespan_builds_real_registered_components_and_disposes(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _runtime_module()
    env = _runtime_env(str(db_url))
    monkeypatch.setattr(os, "environ", env)
    dispose_calls: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def recording_dispose(engine: AsyncEngine) -> None:
        dispose_calls.append(engine)
        await original_dispose(engine)

    monkeypatch.setattr(AsyncEngine, "dispose", recording_dispose)
    app = module.create_runtime_app()
    async with app.router.lifespan_context(app):
        from infra.db.outbox_delivery import OutboxDeliverer
        from infra.db.repositories.notifications import PostgresNotificationDedupStore
        from infra.db.workflow_engine import PostgresWorkflowEngine

        dependencies = get_api_dependencies(_request_for(app))
        assert isinstance(dependencies.workflow_engine, PostgresWorkflowEngine)
        assert isinstance(dependencies.outbox_deliverer, OutboxDeliverer)
        assert isinstance(
            dependencies.notification_dedup_store,
            PostgresNotificationDedupStore,
        )
        assert await app.state.readiness_probe.is_ready() is True

        tenant = TenantId(env["TRADEOS_TENANT_ID"])
        handoff_id = HandoffId(new_id("hnd"))
        event = HandoffRequested(
            tenant_id=tenant,
            occurred_at=_NOW,
            handoff_id=handoff_id,
            opportunity_id=OpportunityId(new_id("opp")),
            assigned_to=EmployeeId(new_id("emp")),
            trigger="manual",
        )
        factory = async_sessionmaker(
            bind=app.state.runtime_engine,
            expire_on_commit=False,
        )
        async with factory() as session:
            await PostgresEventBus(session, tenant, now=lambda: _NOW).publish(event)
            await session.commit()
        assert await dependencies.outbox_deliverer.drain() == 1
        async with factory() as session:
            run = await session.scalar(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == str(tenant),
                    WorkflowRunRow.subject_ref == str(handoff_id),
                )
            )
        assert run is not None
        assert run.workflow_type == "human_handoff"
    assert dispose_calls == [app.state.runtime_engine]


async def test_employee_service_scope_uses_distinct_sessions_and_cleans_up(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _composition_module()
    engine = create_engine_from(str(db_url))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    tenant = TenantId("tenant-runtime-scope")
    repository_sessions: list[AsyncSession] = []
    closed: list[AsyncSession] = []
    rolled_back: list[AsyncSession] = []

    from infra.db.repositories.employees import EmployeeRepositoryImpl

    original_repository_init = EmployeeRepositoryImpl.__init__
    original_close = AsyncSession.close
    original_rollback = AsyncSession.rollback

    def recording_repository_init(self, session, tenant_id) -> None:
        repository_sessions.append(session)
        original_repository_init(self, session, tenant_id)

    async def recording_close(session: AsyncSession) -> None:
        closed.append(session)
        await original_close(session)

    async def recording_rollback(session: AsyncSession) -> None:
        rolled_back.append(session)
        await original_rollback(session)

    monkeypatch.setattr(EmployeeRepositoryImpl, "__init__", recording_repository_init)
    monkeypatch.setattr(AsyncSession, "close", recording_close)
    monkeypatch.setattr(AsyncSession, "rollback", recording_rollback)

    authorizer = Phase1EmployeeAuthorizer(tenant)
    audit = StandardAuditLogger()
    try:
        async with composition.employee_service_scope(
            factory,
            tenant,
            now=lambda: _NOW,
            authorizer=authorizer,
            audit=audit,
        ):
            pass
        async with composition.employee_service_scope(
            factory,
            tenant,
            now=lambda: _NOW,
            authorizer=authorizer,
            audit=audit,
        ):
            pass
        assert len(repository_sessions) == 2
        assert repository_sessions[0] is not repository_sessions[1]

        with pytest.raises(LookupError, match="scope failure"):
            async with composition.employee_service_scope(
                factory,
                tenant,
                now=lambda: _NOW,
                authorizer=authorizer,
                audit=audit,
            ):
                raise LookupError("scope failure")
        failed_session = repository_sessions[-1]
        assert failed_session in rolled_back
        assert failed_session in closed
    finally:
        await engine.dispose()
