"""Tool Gateway 真实 PostgreSQL canonical、重领与对账语义。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import ToolCallEventRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import PreparedToolCall, ToolCallContext, ToolGateway
from tool_gateway.repository import ToolCallId, ToolCallRecord


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _Stage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def check(self, ctx, state):
        del ctx, state
        self.calls += 1


class _RateStage(_Stage):
    def __init__(self, *, fail_record: bool = False) -> None:
        super().__init__("rate_limit")
        self.fail_record = fail_record
        self.recorded: list[str] = []

    async def record_sent(self, ctx, state, provider_ref):
        del ctx, state
        if self.fail_record:
            raise RuntimeError("local-write-marker")
        self.recorded.append(provider_ref)


class _Handler:
    def __init__(self) -> None:
        self.execute_calls = 0

    async def prepare(self, ctx: ToolCallContext) -> PreparedToolCall:
        material = str(ctx.params.get("material", "stable")).encode()
        return PreparedToolCall(
            hashlib.sha256(material).hexdigest(),
            "fp-v1",
            {"attempt": "bound"},
            object(),
        )

    async def execute(self, tenant_id, prepared):
        del tenant_id, prepared
        self.execute_calls += 1
        return {"provider_ref": "gmail_ref_1", "already_existed": False}


def _manifest() -> ToolManifest:
    return ToolManifest(
        tool_id="email.send",
        version="v1",
        description="发送已批准 Campaign 邮件",
        risk_level=RiskLevel.HIGH,
        cost_class=CostClass.LOW,
        requires_approval=False,
        idempotency=IdempotencyRequirement.REQUIRED,
        required_permissions=("outreach:message_send",),
        checks=(
            "tenant",
            "permission",
            "suppression",
            "approval",
            "idempotency",
            "rate_limit",
        ),
    )


def _checks(*, fail_record: bool = False):
    rate = _RateStage(fail_record=fail_record)
    checks = {
        "tenant": _Stage("tenant"),
        "permission": _Stage("permission"),
        "suppression": _Stage("suppression.preflight"),
        "approval": _Stage("approval"),
        "idempotency": _Stage("idempotency"),
        "rate_limit": rate,
    }
    return checks, rate


def _context(tenant: TenantId, key: str, *, material: str = "stable") -> ToolCallContext:
    return ToolCallContext(
        tenant_id=tenant,
        user_id=UserId(new_id("usr")),
        tool_id="email.send",
        params={"attempt_id": new_id("mat"), "material": material},
        idempotency_key=IdempotencyKey(key),
        campaign_ref=new_id("cmp"),
    )


def _gateway(factory, tenant: TenantId, clock: _Clock, *, fail_record: bool = False):
    handler = _Handler()
    registry = ToolRegistry()
    registry.register(_manifest(), handler)
    checks, rate = _checks(fail_record=fail_record)
    gateway = ToolGateway(
        registry,
        checks,
        lambda requested: SqlAlchemyToolGatewayUnitOfWork(
            factory, requested, now=clock.now
        ),
        lease_duration=timedelta(seconds=5),
        lease_owner="worker-1",
        now=clock.now,
        id_factory=new_id,
    )
    return gateway, handler, rate


def _received(tenant: TenantId, call_id: ToolCallId, now: datetime) -> ToolCallRecord:
    return ToolCallRecord(
        tenant_id=tenant,
        tool_call_id=call_id,
        tool_id="email.send",
        tool_version="v1",
        risk_level="high",
        cost_class="low",
        idempotency_key=None,
        request_fingerprint=None,
        fingerprint_version=None,
        status=ToolCallStatus.RECEIVED,
        duplicate_of=None,
        lease_owner=None,
        lease_expires_at=None,
        attempt_count=0,
        run_id=None,
        user_id=UserId(new_id("usr")),
        campaign_id=new_id("cmp"),
        message_attempt_id=new_id("mat"),
        provider_ref=None,
        error_category=None,
        retry_after_at=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


async def test_success_duplicate_conflict_and_safe_stage_events_are_durable(
    db_url: str,
) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = _Clock(datetime(2026, 8, 11, 5, tzinfo=UTC))
    gateway, handler, rate = _gateway(factory, tenant, clock)
    try:
        first = await gateway.invoke(_context(tenant, "pipeline-key"))
        duplicate = await gateway.invoke(_context(tenant, "pipeline-key"))
        conflict = await gateway.invoke(
            _context(tenant, "pipeline-key", material="changed")
        )
        assert first.status is ToolCallStatus.SUCCEEDED
        assert duplicate.status is ToolCallStatus.DUPLICATE
        assert duplicate.tool_call_id == first.tool_call_id
        assert conflict.error_category is ToolErrorCategory.IDEMPOTENCY_CONFLICT
        assert handler.execute_calls == 1
        assert rate.calls == 1 and rate.recorded == ["gmail_ref_1"]
        async with factory() as session:
            rows = (
                await session.execute(
                    select(ToolCallRow).where(ToolCallRow.tenant_id == tenant)
                )
            ).scalars().all()
            events = (
                await session.execute(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
        assert {row.status for row in rows} == {"succeeded", "duplicate", "rejected"}
        canonical_events = {
            (event.stage, event.outcome)
            for event in events
            if event.tool_call_id == first.tool_call_id
        }
        assert {
            ("ledger", "received"),
            ("tenant", "allowed"),
            ("permission", "allowed"),
            ("suppression.preflight", "allowed"),
            ("approval", "allowed"),
            ("idempotency", "claimed"),
            ("rate_limit", "allowed"),
            ("ledger", "executing"),
            ("ledger", "succeeded"),
        } <= canonical_events
        rendered = repr((rows, events))
        for raw in ("changed", "buyer@example.com", "secret subject", "secret body"):
            assert raw not in rendered
    finally:
        await engine.dispose()


async def test_expired_claim_reuses_canonical_but_executing_never_reexecutes(
    db_url: str,
) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = _Clock(datetime(2026, 8, 11, 6, tzinfo=UTC))
    try:
        for status in (ToolCallStatus.CLAIMED, ToolCallStatus.EXECUTING):
            tenant = TenantId(new_id("tn"))
            old_id = ToolCallId(new_id("tcl"))
            async with SqlAlchemyToolGatewayUnitOfWork(
                factory, tenant, now=clock.now
            ) as uow:
                await uow.calls.create_received(_received(tenant, old_id, clock.now()))
                claimed = await uow.calls.claim(
                    tenant,
                    old_id,
                    tool_id="email.send",
                    idempotency_key=IdempotencyKey(f"recover-{status.value}"),
                    request_fingerprint=hashlib.sha256(b"stable").hexdigest(),
                    fingerprint_version="fp-v1",
                    lease_owner="crashed-worker",
                    lease_expires_at=clock.now() + timedelta(seconds=1),
                )
                if status is ToolCallStatus.EXECUTING:
                    await uow.calls.mark_executing(tenant, claimed.canonical.tool_call_id)
            clock.value += timedelta(seconds=2)
            gateway, handler, _rate = _gateway(factory, tenant, clock)
            result = await gateway.invoke(
                _context(tenant, f"recover-{status.value}")
            )
            if status is ToolCallStatus.CLAIMED:
                assert result.status is ToolCallStatus.SUCCEEDED
                assert result.tool_call_id == old_id
                assert handler.execute_calls == 1
            else:
                assert result.status is ToolCallStatus.FAILED_TRANSIENT
                assert result.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
                assert handler.execute_calls == 0
    finally:
        await engine.dispose()


async def test_provider_success_local_completion_failure_is_reconciliation_required(
    db_url: str,
) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = _Clock(datetime(2026, 8, 11, 7, tzinfo=UTC))
    gateway, handler, _rate = _gateway(factory, tenant, clock, fail_record=True)
    try:
        result = await gateway.invoke(_context(tenant, "local-failure"))
        assert result.status is ToolCallStatus.FAILED_TRANSIENT
        assert result.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert handler.execute_calls == 1
        async with factory() as session:
            canonical = (
                await session.execute(
                    select(ToolCallRow).where(
                        ToolCallRow.tenant_id == tenant,
                        ToolCallRow.idempotency_key == "local-failure",
                    )
                )
            ).scalar_one()
        assert canonical.status == "failed_transient"
        assert canonical.error_category == "reconciliation_required"
    finally:
        await engine.dispose()
