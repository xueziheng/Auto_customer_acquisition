"""通知任务 PostgreSQL 持久化、租约和 fencing 契约。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from notification_gateway.models import NotificationPriority
from shared.schemas.identifiers import EmployeeId, TenantId


def _load(name: str):
    try:
        return getattr(importlib.import_module("notification_gateway.jobs"), name)
    except (AttributeError, ModuleNotFoundError) as exc:
        pytest.fail(f"RED：缺少通知任务契约（{exc}）")


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
async def store(db_url: str):
    from infra.db.session import create_engine_from
    try:
        from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：通知任务仓储未创建（{exc}）")
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    clock = _Clock()
    try:
        yield PostgresNotificationJobStore(factory, now=clock.now), clock, engine
    finally:
        await engine.dispose()


def _job(job_id: str = "njb_01"):
    NotificationJob = _load("NotificationJob")
    NotificationContext = _load("NotificationContext")
    NotificationKind = _load("NotificationKind")
    NotificationJobId = importlib.import_module("shared.schemas.identifiers").NotificationJobId
    fingerprint = (job_id[-1] * 64)
    return NotificationJob(NotificationJobId(job_id), TenantId("tn_jobs"), EmployeeId("emp_jobs"), NotificationPriority.URGENT, NotificationContext(NotificationKind.HANDOFF_ESCALATION, f"han_{job_id[-1]}", None, None, 1), fingerprint, "HandoffEscalated", f"handoff:{job_id}", datetime(2026, 8, 14, tzinfo=UTC))


@pytest.mark.asyncio
async def test_enqueue_is_durable_idempotent_and_claim_is_single_owner(store) -> None:
    """唯一 source-event/recipient/kind 键防重，并发 PG claim 只给一个 worker。"""
    job_store, _clock, _engine = store
    job = _job()
    assert await job_store.enqueue(job) is True
    assert await job_store.enqueue(job) is False
    claims = await asyncio.gather(*[job_store.claim_due(TenantId("tn_jobs"), limit=1, lease_owner=f"w{i}") for i in range(20)])
    assert sum(len(batch) for batch in claims) == 1
    assert await job_store.complete(TenantId("tn_jobs"), job.job_id, claim_token="stale") is False


@pytest.mark.asyncio
async def test_claim_expiry_retry_backoff_and_error_are_safe(store) -> None:
    """租约过期可接管；重试退避递增且仅持久化异常类型。"""
    job_store, clock, engine = store
    job = _job("njb_02")
    await job_store.enqueue(job)
    first = (await job_store.claim_due(TenantId("tn_jobs"), limit=1, lease_owner="worker"))[0]
    assert await job_store.retry(TenantId("tn_jobs"), job.job_id, claim_token=first.claim_token, error=RuntimeError("Bearer secret"))
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT available_at, last_error FROM notification_jobs WHERE tenant_id='tn_jobs' AND notification_job_id='njb_02'"))).one()
    assert row.last_error == "RuntimeError"
    assert row.available_at == clock.now() + timedelta(seconds=30)
    clock.advance(30)
    second = (await job_store.claim_due(TenantId("tn_jobs"), limit=1, lease_owner="worker2"))[0]
    assert await job_store.retry(TenantId("tn_jobs"), job.job_id, claim_token=second.claim_token, error=RuntimeError())
    async with engine.connect() as conn:
        available = (await conn.execute(text("SELECT available_at FROM notification_jobs WHERE tenant_id='tn_jobs' AND notification_job_id='njb_02'"))).scalar_one()
    assert available == clock.now() + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_claim_can_recover_expired_processing_lease(store) -> None:
    """崩溃遗留的 processing 租约到期后可由其它 worker 认领。"""
    job_store, clock, _engine = store
    job = _job("njb_03")
    await job_store.enqueue(job)
    first = (await job_store.claim_due(TenantId("tn_jobs"), limit=1, lease_owner="w1"))[0]
    clock.advance(301)
    second = (await job_store.claim_due(TenantId("tn_jobs"), limit=1, lease_owner="w2"))[0]
    assert second.claim_token != first.claim_token
