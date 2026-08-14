"""真实 PostgreSQL 工作流对认证检查的 durable retry/幂等合同。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.sending_identity.permissions import (
    Actor,
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityScope,
)
from domains.sending_identity.schemas import DomainRole, IdentityRegisterRequest
from domains.sending_identity.service import SendingIdentityUnitOfWorkFactory
from shared.errors import TransientError
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    IdempotencyKey,
    RunId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from workflows.engine.runner import StepStatus, WorkflowRun

_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
_TENANT = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")
_IDENTITY = SendingIdentityId("sid_01K2C5R6J7ABCDEFGHJKMNPQRS")
_REQUEST = AuthenticationCheckRequestId("acr_01K2C5R6J7ABCDEFGHJKMNPQRS")


@pytest_asyncio.fixture
async def workflow_db_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """函数级 engine 与当前 pytest loop 同生共死，避免 asyncpg 跨 loop。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _flow():
    try:
        return importlib.import_module("workflows.sending_identity_auth.flow")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：认证 workflow 尚未创建（{exc.name}）")


def _repository():
    return importlib.import_module("domains.sending_identity.repository")


def _facts():
    contracts = importlib.import_module("shared.schemas.dns_auth")
    return contracts.DnsAuthenticationFacts(_NOW, True, True, True, (), "b" * 64)


class _Sending:
    def __init__(self) -> None:
        self.transitions: list[object] = []
        self.results: list[object] = []
        self.status = _repository().AuthenticationCheckRequestStatus.REQUESTED

    async def get(self, tenant_id, identity_id, *, actor):
        return SimpleNamespace(
            tenant_id=tenant_id,
            identity_id=identity_id,
            domain="example.co.uk",
        )

    async def transition_authentication_check_request(
        self, tenant_id, request_id, status, *, actor
    ):
        self.transitions.append(status)
        self.status = status
        return SimpleNamespace(
            request_id=request_id,
            tenant_id=tenant_id,
            sending_identity_id=_IDENTITY,
            request_key=IdempotencyKey("request-key"),
            status=status,
            requested_at=_NOW,
            completed_at=None,
        )

    async def get_authentication_check_request(
        self, tenant_id, request_id, *, actor
    ):
        del actor
        return SimpleNamespace(
            request_id=request_id,
            tenant_id=tenant_id,
            sending_identity_id=_IDENTITY,
            request_key=IdempotencyKey("request-key"),
            status=self.status,
            requested_at=_NOW,
            completed_at=None,
        )

    async def record_authentication_result(
        self, tenant_id, identity_id, result, *, actor
    ):
        self.results.append(result)


class _RestartingTool:
    calls = 0

    async def check(self, **kwargs):
        type(self).calls += 1
        if type(self).calls == 1:
            raise TransientError("raw resolver secret")
        return _facts()


class _OneShotTool:
    def __init__(self) -> None:
        self.calls = 0

    async def check(self, **kwargs):
        del kwargs
        self.calls += 1
        return _facts()


def _real_service(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    *,
    now: datetime = _NOW,
):
    from domains.sending_identity.permissions import StandardAuditLogger
    from domains.sending_identity.service_impl import SendingIdentityServiceImpl
    from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork

    return SendingIdentityServiceImpl(
        cast(
            SendingIdentityUnitOfWorkFactory,
            lambda requested_tenant: SqlAlchemySendingIdentityUnitOfWork(
                factory, requested_tenant, now=lambda: now
            ),
        ),
        Phase1SendingIdentityAuthorizer(tenant_id),
        StandardAuditLogger(),
        now=lambda: now,
    )


def _boss() -> Actor:
    return Actor(
        "boss_auth_workflow",
        SendingIdentityScope(level=ScopeLevel.TENANT),
        "boss",
    )


async def test_retryable_dns_work_survives_engine_restart_and_completes(
    workflow_db_engine,
) -> None:
    """临时 resolver 故障必须留下 due step，新引擎实例可恢复且不泄漏异常。"""
    from infra.db.workflow_engine import PostgresWorkflowEngine

    flow = _flow()
    factory = async_sessionmaker(bind=workflow_db_engine, expire_on_commit=False)
    async with workflow_db_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
            {"tenant": str(_TENANT)},
        )
    _RestartingTool.calls = 0
    sending = _Sending()
    first_handler = flow.DnsAuthenticationStep(
        sending, _RestartingTool(), dkim_selector="s1"
    )
    first = PostgresWorkflowEngine(
        factory,
        {"sending_identity_auth.check": first_handler},
        now=lambda: _NOW,
    )
    first.register(flow.build_sending_identity_auth_definition())
    run_id = await first.start(
        _TENANT,
        "sending_identity_authentication",
        str(_REQUEST),
        {"request_id": str(_REQUEST), "sending_identity_id": str(_IDENTITY)},
        f"auth:{_REQUEST}",
    )
    assert await first.poll_due(_TENANT, 10) == 1
    async with workflow_db_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT status,next_poll_at,last_error FROM workflow_runs "
                    "WHERE tenant_id=:tenant AND run_id=:run"
                ),
                {"tenant": str(_TENANT), "run": str(run_id)},
            )
        ).one()
    assert row.status == "running"
    assert row.next_poll_at == _NOW + timedelta(seconds=30)
    assert row.last_error == "TransientError"
    assert "secret" not in row.last_error

    second_handler = flow.DnsAuthenticationStep(
        sending, _RestartingTool(), dkim_selector="s1"
    )
    second = PostgresWorkflowEngine(
        factory,
        {"sending_identity_auth.check": second_handler},
        now=lambda: _NOW + timedelta(seconds=31),
    )
    second.register(flow.build_sending_identity_auth_definition())
    duplicate = await second.start(
        _TENANT,
        "sending_identity_authentication",
        str(_REQUEST),
        {"request_id": str(_REQUEST), "sending_identity_id": str(_IDENTITY)},
        f"auth:{_REQUEST}",
    )
    assert duplicate == run_id
    assert await second.poll_due(_TENANT, 10) == 1
    async with workflow_db_engine.connect() as connection:
        status = (
            await connection.execute(
                text(
                    "SELECT status FROM workflow_runs "
                    "WHERE tenant_id=:tenant AND run_id=:run"
                ),
                {"tenant": str(_TENANT), "run": str(run_id)},
            )
        ).scalar_one()
    assert status == "completed"
    assert _RestartingTool.calls == 2
    assert len(sending.results) == 1


async def test_real_domain_commit_before_workflow_commit_replays_without_dns(
    workflow_db_engine: AsyncEngine,
) -> None:
    """真实域事务已提交而 workflow 未提交时，恢复只收敛 workflow。"""
    from infra.db.workflow_engine import PostgresWorkflowEngine

    flow = _flow()
    factory = async_sessionmaker(bind=workflow_db_engine, expire_on_commit=False)
    tenant_id = TenantId(new_id("tn"))
    service = _real_service(factory, tenant_id)
    identity_id = await service.register(
        tenant_id,
        IdentityRegisterRequest(
            address="sales@auth-crash-window.example",
            domain="auth-crash-window.example",
            role=DomainRole.COLD_OUTREACH,
        ),
        actor=_boss(),
    )
    request = await service.request_authentication_check(
        tenant_id,
        identity_id,
        IdempotencyKey("auth-crash-window"),
        actor=_boss(),
    )
    tool = _OneShotTool()
    handler = flow.DnsAuthenticationStep(service, tool, dkim_selector="s1")
    engine = PostgresWorkflowEngine(
        factory,
        {"sending_identity_auth.check": handler},
        now=lambda: _NOW,
    )
    engine.register(flow.build_sending_identity_auth_definition())
    run_id = await engine.start(
        tenant_id,
        "sending_identity_authentication",
        str(request.request_id),
        {
            "request_id": str(request.request_id),
            "sending_identity_id": str(identity_id),
        },
        f"auth:{request.request_id}",
    )

    # 模拟 handler 域事务提交后、engine step 事务提交前进程崩溃。
    outcome = await handler.execute(
        WorkflowRun(
            RunId(run_id),
            tenant_id,
            "sending_identity_authentication",
            1,
            str(request.request_id),
            "check",
            StepStatus.RUNNING,
            _NOW,
            context={
                "request_id": str(request.request_id),
                "sending_identity_id": str(identity_id),
            },
        )
    )
    assert outcome == ("complete", None, {})
    assert tool.calls == 1

    assert await engine.poll_due(tenant_id, 10) == 1
    async with workflow_db_engine.connect() as connection:
        status = (
            await connection.execute(
                text(
                    "SELECT status FROM workflow_runs "
                    "WHERE tenant_id=:tenant AND run_id=:run"
                ),
                {"tenant": str(tenant_id), "run": str(run_id)},
            )
        ).scalar_one()
    assert status == "completed"
    assert tool.calls == 1


async def test_same_second_identity_and_dns_capsule_complete_real_domain_write(
    workflow_db_engine: AsyncEngine,
) -> None:
    """同秒注册后的 DNS 微秒事实经 capsule 往返仍不得变成过早结果。"""
    handler_module = importlib.import_module("tool_gateway.handlers.dns_auth")
    flow = _flow()
    factory = async_sessionmaker(bind=workflow_db_engine, expire_on_commit=False)
    tenant_id = TenantId(new_id("tn"))
    created_at = _NOW.replace(microsecond=900_000)
    checked_at = _NOW.replace(microsecond=950_000)
    service = _real_service(factory, tenant_id, now=created_at)
    identity_id = await service.register(
        tenant_id,
        IdentityRegisterRequest(
            address="sales@auth-microsecond.example",
            domain="auth-microsecond.example",
            role=DomainRole.COLD_OUTREACH,
        ),
        actor=_boss(),
    )
    request = await service.request_authentication_check(
        tenant_id,
        identity_id,
        IdempotencyKey("auth-microsecond"),
        actor=_boss(),
    )
    contracts = importlib.import_module("shared.schemas.dns_auth")
    facts = contracts.DnsAuthenticationFacts(
        checked_at, True, True, True, (), "9" * 64
    )

    class CapsuleTool:
        async def check(self, **kwargs):
            del kwargs
            return handler_module._decode_facts(handler_module._encode_facts(facts))

    outcome = await flow.DnsAuthenticationStep(
        service, CapsuleTool(), dkim_selector="s1"
    ).execute(
        WorkflowRun(
            RunId(new_id("run")),
            tenant_id,
            "sending_identity_authentication",
            1,
            str(request.request_id),
            "check",
            StepStatus.RUNNING,
            created_at,
            context={
                "request_id": str(request.request_id),
                "sending_identity_id": str(identity_id),
            },
        )
    )
    assert outcome == ("complete", None, {})
