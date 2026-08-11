"""Tool Call durable ledger 的真实 PostgreSQL 并发与租约语义。"""

from __future__ import annotations

import asyncio
import importlib
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory

_NOW = datetime(2026, 8, 11, 3, tzinfo=UTC)


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _contracts():
    try:
        repository = importlib.import_module("tool_gateway.repository")
        uow = importlib.import_module("infra.db.tool_gateway_uow")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：Tool Call repository/UoW 尚未实现（{exc}）")
    return repository, uow.SqlAlchemyToolGatewayUnitOfWork


def _record(repository, tenant: TenantId, call_id: str, now: datetime):
    return repository.ToolCallRecord(
        tenant_id=tenant,
        tool_call_id=repository.ToolCallId(call_id),
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
        user_id=UserId("usr_00000000000000000000000000"),
        campaign_id="cmp_00000000000000000000000000",
        message_attempt_id="mat_00000000000000000000000000",
        provider_ref=None,
        error_category=None,
        retry_after_at=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def test_record_contract_rejects_impossible_state_and_secret_event_value() -> None:
    repository, _ = _contracts()
    tenant = TenantId("tn_tool_contract")
    received = _record(repository, tenant, new_id("tcl"), _NOW)
    with pytest.raises(ValidationError):
        replace(received, status=ToolCallStatus.CLAIMED)
    with pytest.raises(ValidationError):
        repository.ToolCallEventRecord(
            tenant_id=tenant,
            event_id=new_id("tce"),
            tool_call_id=received.tool_call_id,
            stage="idempotency",
            outcome="claimed",
            rule="canonical_key",
            category=None,
            actor_id="usr_00000000000000000000000000",
            run_id=None,
            campaign_id=None,
            message_attempt_id=None,
            occurred_at=_NOW,
            duration_ms=1,
            cost_note="secret_customer_text",
        )


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    key: str,
    fingerprint: str,
    *,
    now: datetime = _NOW,
    lease_for: timedelta = timedelta(minutes=5),
    fingerprint_version: str = "fp-v1",
):
    repository, uow_class = _contracts()
    call_id = repository.ToolCallId(new_id("tcl"))
    async with uow_class(session_factory, tenant, now=lambda: now) as uow:
        await uow.calls.create_received(_record(repository, tenant, call_id, now))
        result = await uow.calls.claim(
            tenant,
            call_id,
            tool_id="email.send",
            idempotency_key=IdempotencyKey(key),
            request_fingerprint=fingerprint,
            fingerprint_version=fingerprint_version,
            lease_owner="worker-1",
            lease_expires_at=now + lease_for,
        )
    return call_id, result


async def test_twenty_concurrent_same_key_have_one_canonical_claim(
    engine_fx: AsyncEngine,
) -> None:
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    tenant = TenantId("tn_tool_race")
    results = await asyncio.gather(
        *(_claim(sf, tenant, "same-key", "a" * 64) for _ in range(20))
    )
    assert Counter(item[1].status for item in results) == {
        _contracts()[0].ClaimStatus.CLAIMED: 1,
        _contracts()[0].ClaimStatus.IN_PROGRESS: 19,
    }
    canonical_ids = {item[1].canonical.tool_call_id for item in results}
    assert len(canonical_ids) == 1


async def test_same_key_different_fingerprint_or_version_is_conflict(
    engine_fx: AsyncEngine,
) -> None:
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    tenant = TenantId("tn_tool_conflict")
    _, first = await _claim(sf, tenant, "conflict-key", "a" * 64)
    _, second = await _claim(sf, tenant, "conflict-key", "b" * 64)

    assert first.status is _contracts()[0].ClaimStatus.CLAIMED
    assert second.status is _contracts()[0].ClaimStatus.CONFLICT
    assert second.canonical.tool_call_id == first.canonical.tool_call_id

    version_tenant = TenantId("tn_tool_version_conflict")
    _, version_first = await _claim(
        sf, version_tenant, "version-key", "c" * 64, fingerprint_version="fp-v1"
    )
    _, version_second = await _claim(
        sf, version_tenant, "version-key", "c" * 64, fingerprint_version="fp-v2"
    )
    assert version_first.status is _contracts()[0].ClaimStatus.CLAIMED
    assert version_second.status is _contracts()[0].ClaimStatus.CONFLICT


async def test_completed_call_returns_duplicate_without_new_execution(
    engine_fx: AsyncEngine,
) -> None:
    repository, uow_class = _contracts()
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    tenant = TenantId("tn_tool_duplicate")
    _, first = await _claim(sf, tenant, "duplicate-key", "c" * 64)
    async with uow_class(sf, tenant, now=lambda: _NOW) as uow:
        await uow.calls.mark_executing(tenant, first.canonical.tool_call_id)
    async with uow_class(sf, tenant, now=lambda: _NOW) as uow:
        await uow.calls.complete(
            tenant,
            first.canonical.tool_call_id,
            status=ToolCallStatus.SUCCEEDED,
            provider_ref="gmail-msg-001",
            error_category=None,
            retry_after_at=None,
        )
    duplicate_id, duplicate = await _claim(
        sf, tenant, "duplicate-key", "c" * 64, now=_NOW + timedelta(minutes=1)
    )

    assert duplicate.status is repository.ClaimStatus.DUPLICATE
    assert duplicate.canonical.status is ToolCallStatus.SUCCEEDED
    async with uow_class(sf, tenant, now=lambda: _NOW) as uow:
        invocation = await uow.calls.get(tenant, duplicate_id)
    assert invocation is not None
    assert invocation.status is ToolCallStatus.DUPLICATE
    assert invocation.duplicate_of == first.canonical.tool_call_id


async def test_expired_claimed_lease_reclaims_but_executing_never_auto_reclaims(
    engine_fx: AsyncEngine,
) -> None:
    repository, uow_class = _contracts()
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    tenant = TenantId("tn_tool_lease")
    _, first = await _claim(
        sf, tenant, "lease-key", "d" * 64, lease_for=timedelta(seconds=1)
    )
    _, reclaimed = await _claim(
        sf,
        tenant,
        "lease-key",
        "d" * 64,
        now=_NOW + timedelta(seconds=2),
    )
    assert reclaimed.status is repository.ClaimStatus.CLAIMED
    assert reclaimed.canonical.tool_call_id == first.canonical.tool_call_id
    assert reclaimed.canonical.attempt_count == 2

    async with uow_class(sf, tenant, now=lambda: _NOW + timedelta(seconds=2)) as uow:
        await uow.calls.mark_executing(tenant, first.canonical.tool_call_id)
    _, blocked = await _claim(
        sf,
        tenant,
        "lease-key",
        "d" * 64,
        now=_NOW + timedelta(hours=1),
    )
    assert blocked.status is repository.ClaimStatus.IN_PROGRESS
    assert blocked.canonical.status is ToolCallStatus.EXECUTING


async def test_same_key_is_isolated_by_tenant(engine_fx: AsyncEngine) -> None:
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    results = await asyncio.gather(
        _claim(sf, TenantId("tn_tool_a"), "shared-key", "e" * 64),
        _claim(sf, TenantId("tn_tool_b"), "shared-key", "e" * 64),
    )
    assert [item[1].status for item in results] == [
        _contracts()[0].ClaimStatus.CLAIMED,
        _contracts()[0].ClaimStatus.CLAIMED,
    ]
    assert results[0][1].canonical.tool_call_id != results[1][1].canonical.tool_call_id


async def test_repository_rejects_cross_tenant_calls_and_raw_safe_refs(
    engine_fx: AsyncEngine,
) -> None:
    repository, uow_class = _contracts()
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    tenant = TenantId("tn_tool_bound")
    other = TenantId("tn_tool_other")
    async with uow_class(sf, tenant, now=lambda: _NOW) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.calls.create_received(
                _record(repository, other, new_id("tcl"), _NOW)
            )
        with pytest.raises(ValidationError):
            await uow.calls.complete(
                tenant,
                repository.ToolCallId(new_id("tcl")),
                status=ToolCallStatus.SUCCEEDED,
                provider_ref="buyer@example.com",
                error_category=None,
                retry_after_at=None,
            )


async def test_event_roundtrip_and_rows_never_contain_raw_content(
    engine_fx: AsyncEngine,
) -> None:
    repository, uow_class = _contracts()
    from infra.db.tables import ToolCallEventRow, ToolCallRow

    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    tenant = TenantId("tn_tool_event")
    call_id, claimed = await _claim(sf, tenant, "event-key", "f" * 64)
    event = repository.ToolCallEventRecord(
        tenant_id=tenant,
        event_id=new_id("tce"),
        tool_call_id=claimed.canonical.tool_call_id,
        stage="idempotency",
        outcome="claimed",
        rule="canonical_key",
        category=None,
        actor_id="usr_00000000000000000000000000",
        run_id=None,
        campaign_id="cmp_00000000000000000000000000",
        message_attempt_id="mat_00000000000000000000000000",
        occurred_at=_NOW,
        duration_ms=3,
        cost_note="low",
    )
    async with uow_class(sf, tenant, now=lambda: _NOW) as uow:
        await uow.calls.append_event(event)
        loaded = await uow.calls.get(tenant, claimed.canonical.tool_call_id)
    assert loaded == claimed.canonical

    async with sf() as session:
        call_row = (
            await session.execute(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == tenant,
                    ToolCallRow.tool_call_id == claimed.canonical.tool_call_id,
                )
            )
        ).scalar_one()
        event_row = (
            await session.execute(
                select(ToolCallEventRow).where(
                    ToolCallEventRow.tenant_id == tenant,
                    ToolCallEventRow.tool_call_id == claimed.canonical.tool_call_id,
                )
            )
        ).scalar_one()
    assert call_id == claimed.canonical.tool_call_id
    rendered = repr((call_row.__dict__, event_row.__dict__))
    for raw in ("buyer@example.com", "secret subject", "secret body", "Bearer token"):
        assert raw not in rendered


async def test_failed_transient_can_be_reclaimed_after_lease_expiry(
    engine_fx: AsyncEngine,
) -> None:
    repository, uow_class = _contracts()
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    tenant = TenantId("tn_tool_transient")
    _, first = await _claim(
        sf, tenant, "transient-key", "1" * 64, lease_for=timedelta(seconds=1)
    )
    async with uow_class(sf, tenant, now=lambda: _NOW) as uow:
        await uow.calls.complete(
            tenant,
            first.canonical.tool_call_id,
            status=ToolCallStatus.FAILED_TRANSIENT,
            provider_ref=None,
            error_category=ToolErrorCategory.RATE_LIMITED,
            retry_after_at=_NOW + timedelta(seconds=1),
        )
    _, reclaimed = await _claim(
        sf,
        tenant,
        "transient-key",
        "1" * 64,
        now=_NOW + timedelta(seconds=2),
    )
    assert reclaimed.status is repository.ClaimStatus.CLAIMED
    assert reclaimed.canonical.attempt_count == 2
