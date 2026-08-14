"""通知 worker 与真实 PostgreSQL job 租约的集成语义。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.session import create_engine_from
from notification_gateway.jobs import (
    NotificationContext,
    NotificationJob,
    NotificationKind,
)
from notification_gateway.models import Notification, NotificationPriority
from shared.errors import PolicyViolation, TransientError
from shared.schemas.identifiers import (
    EmployeeId,
    NotificationJobId,
    TenantId,
    new_id,
)


def _load(module_name: str, symbol: str):
    try:
        return getattr(importlib.import_module(module_name), symbol)
    except (AttributeError, ModuleNotFoundError) as exc:
        pytest.fail(f"RED：通知 worker {symbol} 尚未实现（{exc}）")


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class _Renderer:
    def render(self, claim) -> Notification:
        return Notification(
            claim.tenant_id,
            claim.recipient,
            claim.priority,
            "固定标题",
            claim.context,
            claim.source_event,
            claim.dedup_key,
            source_job_id=claim.job_id,
        )


class _Router:
    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = outcomes

    async def dispatch(self, _notification: Notification) -> None:
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


def _job(
    tenant: TenantId,
    employee: EmployeeId,
    clock: _Clock,
    index: int,
) -> NotificationJob:
    return NotificationJob(
        NotificationJobId(new_id("njb")),
        tenant,
        employee,
        NotificationPriority.URGENT,
        NotificationContext(
            NotificationKind.COMMITMENT_OVERDUE,
            f"com_{index}",
            None,
            None,
            None,
        ),
        f"{index:x}".rjust(64, "0"),
        "CommitmentOverdue",
        f"worker:{index}",
        clock.now(),
    )


def _config(tenant: TenantId, db_url: str, owner: str):
    Config = _load(
        "apps.notification_worker.config", "NotificationWorkerConfig"
    )
    return Config(
        SecretStr(db_url), tenant, 1, 20, 8093, owner
    )


@pytest.mark.asyncio
async def test_real_postgres_multiworker_skip_locked_claims_each_job_once(
    db_url: str,
) -> None:
    """多副本同时 claim 不得重复取得同一 job。"""
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    clock = _Clock()
    store = PostgresNotificationJobStore(factory, now=clock.now)
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    jobs = [_job(tenant, employee, clock, index) for index in range(1, 11)]
    try:
        for job in jobs:
            assert await store.enqueue(job)
        claims = await asyncio.gather(
            *(
                store.claim_due(tenant, limit=1, lease_owner=f"worker-{index}")
                for index in range(20)
            )
        )
        claimed_ids = [claim.job_id for batch in claims for claim in batch]
        assert len(claimed_ids) == 10
        assert len(set(claimed_ids)) == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_worker_retries_rejects_completes_and_preserves_stale_fencing(
    db_url: str,
) -> None:
    """每个 router 结果必须落成对应 job 状态，旧 token 不能覆盖。"""
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    clock = _Clock()
    store = PostgresNotificationJobStore(factory, now=clock.now)
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    jobs = [_job(tenant, employee, clock, index) for index in range(1, 5)]
    try:
        for job in jobs:
            assert await store.enqueue(job)
        Runtime = _load(
            "apps.notification_worker.runtime", "NotificationWorkerRuntime"
        )
        run_worker = _load(
            "apps.notification_worker.runtime", "run_notification_worker"
        )
        HealthState = _load(
            "apps.notification_worker.health", "NotificationHealthState"
        )
        runtime = Runtime(
            store,
            _Router(
                [
                    TransientError("private"),
                    PolicyViolation("private"),
                    RuntimeError("private"),
                    None,
                ]
            ),
            _Renderer(),
            _config(tenant, db_url, "worker-a"),
            HealthState(),
        )
        stop = asyncio.Event()

        async def wait(_seconds: int, event: asyncio.Event) -> None:
            event.set()

        result = await run_worker(runtime, stop_event=stop, wait=wait)
        assert result.jobs_completed == 1
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT status,last_error,lease_token FROM notification_jobs "
                        "WHERE tenant_id=:tenant ORDER BY created_at,notification_job_id"
                    ),
                    {"tenant": str(tenant)},
                )
            ).all()
        assert [row.status for row in rows].count("completed") == 1
        assert [row.status for row in rows].count("rejected") == 1
        assert [row.status for row in rows].count("pending") == 2
        assert {row.last_error for row in rows if row.last_error} == {
            "PolicyViolation",
            "RuntimeError",
            "TransientError",
        }
        assert all(row.lease_token is None for row in rows)
        assert not await store.complete(
            tenant, jobs[-1].job_id, claim_token="stale-token"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_crashed_claim_stays_leased_until_expiry_then_is_reclaimed(
    db_url: str,
) -> None:
    """worker 在持久化结果前死亡时，租约到期前不可重复投递。"""
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    clock = _Clock()
    store = PostgresNotificationJobStore(factory, now=clock.now)
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    job = _job(tenant, employee, clock, 15)
    try:
        assert await store.enqueue(job)
        first = (await store.claim_due(tenant, limit=1, lease_owner="dead-worker"))[0]
        assert await store.claim_due(tenant, limit=1, lease_owner="other") == ()
        clock.advance(299)
        assert await store.claim_due(tenant, limit=1, lease_owner="other") == ()
        clock.advance(1)
        second = (await store.claim_due(tenant, limit=1, lease_owner="other"))[0]
        assert second.job_id == first.job_id
        assert second.claim_token != first.claim_token
        assert not await store.complete(
            tenant, first.job_id, claim_token=first.claim_token
        )
        assert await store.complete(
            tenant, second.job_id, claim_token=second.claim_token
        )
    finally:
        await engine.dispose()
