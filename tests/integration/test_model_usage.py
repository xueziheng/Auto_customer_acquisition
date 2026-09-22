"""模型配额必须在真实 PG 独立连接竞争，不能由进程内计数保证。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from shared.schemas.identifiers import (
    EmployeeId,
    RunId,
    TenantId,
    UserId,
    new_id,
)
from shared.schemas.model_invocation import InvocationIdentity, ModelLimits, ModelUsage


def limits(**changes):
    return ModelLimits.model_validate(
        {
            "window_seconds": 86400,
            "tenant_calls": 1,
            "employee_calls": 1,
            "tenant_concurrency": 1,
            "employee_concurrency": 1,
            "max_input_bytes": 4096,
            "max_output_tokens": 64,
            "timeout_seconds": 5,
            **changes,
        }
    )


def identity(tenant, employee="emp_test", run=None, version="test-v1"):
    return InvocationIdentity(
        tenant_id=tenant,
        user_id=UserId("usr_test"),
        employee_id=EmployeeId(employee),
        run_id=RunId(run or new_id("run")),
        capability="product_help",
        configuration_version=version,
        sequence=0,
    )


async def test_last_quota_is_atomic_and_replay_does_not_consume(integration_engine):
    from infra.db.model_usage import SqlModelUsageRepository

    repo = SqlModelUsageRepository(
        async_sessionmaker(integration_engine, expire_on_commit=False)
    )
    tenant = TenantId(new_id("tn"))
    now = datetime.now(UTC)
    first = identity(tenant)
    second = identity(tenant, "emp_other")
    results = await asyncio.gather(
        repo.reserve(first, "a" * 64, limits(), now, model="test-model"),
        repo.reserve(second, "b" * 64, limits(), now, model="test-model"),
    )
    assert sorted(r.outcome for r in results) == ["limited", "reserved"]
    winner = first if results[0].outcome == "reserved" else second
    digest = "a" * 64 if winner is first else "b" * 64
    replay = await repo.reserve(winner, digest, limits(), now, model="test-model")
    assert replay.outcome == "duplicate"
    assert replay.invocation_id == next(
        r.invocation_id for r in results if r.outcome == "reserved"
    )
    conflict = await repo.reserve(winner, "c" * 64, limits(), now, model="test-model")
    assert conflict.outcome == "conflict"


async def test_unknown_survives_restart_and_midnight(integration_engine):
    from infra.db.model_usage import SqlModelUsageRepository

    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    repo = SqlModelUsageRepository(sessions)
    tenant = TenantId(new_id("tn"))
    start = datetime(2026, 9, 22, 23, 59, tzinfo=UTC)
    claim = await repo.reserve(
        identity(tenant), "a" * 64, limits(), start, model="test-model"
    )
    await repo.mark_dispatched(tenant, claim.invocation_id)
    await repo.finish(
        tenant,
        claim.invocation_id,
        ModelUsage(input_tokens=None, cached_input_tokens=None, output_tokens=None),
        "unknown",
    )
    restarted = SqlModelUsageRepository(sessions)
    denied = await restarted.reserve(
        identity(tenant),
        "b" * 64,
        limits(),
        start + timedelta(minutes=2),
        model="test-model",
    )
    assert denied.outcome == "limited"
    view = await restarted.get(tenant, claim.invocation_id)
    assert view.state == "unknown" and view.usage.input_tokens is None
    await restarted.release_unknown_slot(
        tenant,
        claim.invocation_id,
        UserId("usr_operator"),
        "operator_confirmed_stopped",
    )
    allowed = await restarted.reserve(
        identity(tenant),
        "b" * 64,
        limits(),
        start + timedelta(minutes=2),
        model="test-model",
    )
    assert allowed.outcome == "reserved"
    assert (await restarted.get(tenant, claim.invocation_id)).state == "unknown"


async def test_completed_call_stays_in_original_window_and_tenant(integration_engine):
    from infra.db.model_usage import SqlModelUsageRepository

    repo = SqlModelUsageRepository(
        async_sessionmaker(integration_engine, expire_on_commit=False)
    )
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    now = datetime(2026, 9, 22, 23, 59, tzinfo=UTC)
    claim = await repo.reserve(
        identity(tenant), "a" * 64, limits(), now, model="test-model"
    )
    await repo.mark_dispatched(tenant, claim.invocation_id)
    usage = ModelUsage(input_tokens=10, cached_input_tokens=2, output_tokens=3)
    await repo.finish(tenant, claim.invocation_id, usage, "succeeded")
    await repo.finish(tenant, claim.invocation_id, usage, "succeeded")
    assert (
        await repo.reserve(
            identity(tenant, version="v2"), "b" * 64, limits(), now, model="test-model"
        )
    ).outcome == "limited"
    assert (
        await repo.reserve(
            identity(tenant),
            "c" * 64,
            limits(),
            now + timedelta(minutes=2),
            model="test-model",
        )
    ).outcome == "reserved"
    assert (
        await repo.reserve(identity(other), "d" * 64, limits(), now, model="test-model")
    ).outcome == "reserved"
    with pytest.raises(ValueError):
        await repo.get(other, claim.invocation_id)


async def test_predispatch_rejection_releases_budget_and_conflicting_finish_fails(
    integration_engine,
):
    from infra.db.model_usage import SqlModelUsageRepository

    repo = SqlModelUsageRepository(
        async_sessionmaker(integration_engine, expire_on_commit=False)
    )
    tenant = TenantId(new_id("tn"))
    now = datetime.now(UTC)
    claim = await repo.reserve(
        identity(tenant), "a" * 64, limits(), now, model="test-model"
    )
    usage = ModelUsage(input_tokens=None, cached_input_tokens=None, output_tokens=None)
    await repo.finish(tenant, claim.invocation_id, usage, "rejected")
    assert (
        await repo.reserve(
            identity(tenant), "b" * 64, limits(), now, model="test-model"
        )
    ).outcome == "reserved"
    with pytest.raises(ValueError):
        await repo.mark_dispatched(tenant, claim.invocation_id)


async def test_employee_limit_and_independent_workflow_lock(integration_engine):
    from sqlalchemy import select

    from infra.db.model_usage import SqlModelUsageRepository
    from infra.db.tables import WorkflowRunRow
    from tests.integration.test_search_quota import _workflow_run

    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    repo = SqlModelUsageRepository(sessions)
    tenant = TenantId(new_id("tn"))
    run = await _workflow_run(sessions, tenant)
    policy = limits(tenant_calls=10, tenant_concurrency=10, employee_calls=1)
    now = datetime.now(UTC)
    async with sessions() as holder, holder.begin():
        await holder.scalar(
            select(WorkflowRunRow)
            .where(WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.run_id == run)
            .with_for_update()
        )
        claim = await asyncio.wait_for(
            repo.reserve(
                identity(tenant, run=run), "a" * 64, policy, now, model="test-model"
            ),
            timeout=2,
        )
    assert claim.outcome == "reserved"
    blocked = await repo.reserve(
        identity(tenant), "b" * 64, policy, now, model="test-model"
    )
    assert blocked.outcome == "limited"
    allowed = await repo.reserve(
        identity(tenant, "emp_other"), "c" * 64, policy, now, model="test-model"
    )
    assert allowed.outcome == "reserved"
