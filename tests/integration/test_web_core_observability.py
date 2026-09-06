"""观测读模型：真实PG、canonical去重、租户与未知输入；数据是仓储边界合成夹具。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.run_audit import PostgresRunAuditRepository
from infra.db.tables import HandoffRow, OpportunityRow, ValidatedNeedRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId
from workflows.engine.audit import (
    Phase1RunAuditAuthorizer,
    RunAuditActor,
    RunAuditService,
)

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


@pytest.fixture
async def observation(integration_engine):
    tenant = TenantId("tn_" + uuid4().hex[:24])
    other = TenantId("tn_" + uuid4().hex[:24])
    async with integration_engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection) as session:
            for index, (scope, state, created) in enumerate(
                [
                    (tenant, "validated", NOW - timedelta(hours=2)),
                    (tenant, "withdrawn", NOW - timedelta(hours=2)),
                    (tenant, "validated", NOW),
                    (other, "validated", NOW - timedelta(hours=2)),
                ]
            ):
                session.add(
                    ValidatedNeedRow(
                        tenant_id=scope,
                        need_id=f"need_{index}",
                        account_id=f"account_{index}",
                        product_category={},
                        source_message_id=f"message_{index}",
                        status=state,
                        created_at=created,
                    )
                )
            session.add(
                OpportunityRow(
                    tenant_id=tenant,
                    opportunity_id="opp_window",
                    account_id="account_0",
                    account_name="合成",
                    country="合成",
                    need_id="need_0",
                    product_category="合成",
                    created_at=NOW - timedelta(hours=2),
                )
            )
            await session.flush()
            for index in range(3):
                session.add(
                    HandoffRow(
                        tenant_id=tenant,
                        handoff_id=f"hand_{index}",
                        opportunity_id="opp_window",
                        trigger="high_value",
                        state="accepted" if index < 2 else "requested",
                        requested_at=NOW - timedelta(hours=1),
                        accepted_at=NOW - timedelta(minutes=30) if index < 2 else None,
                        account_name="合成",
                        country="合成",
                        why_valuable="正文不可投影",
                        customer_verbatim="正文不可投影",
                        assigned_to="emp_handler",
                    )
                )
            await session.flush()

            @asynccontextmanager
            async def scope():
                yield session

            repository = PostgresRunAuditRepository(scope)
            yield repository, tenant, other, session
        await transaction.rollback()


async def test_canonical_window_replay_and_missing_costs(observation):
    repo, tenant, other, _ = observation
    kwargs = {"start": NOW - timedelta(days=1), "end": NOW, "observed_at": NOW}
    result = await repo.get_observability(tenant, **kwargs)
    replay = await repo.get_observability(tenant, **kwargs)
    assert result == replay
    stages = {row.stage: row for row in result.stages}
    assert stages["validated_need"].count == 1  # withdrawn和end边界排除
    assert stages["opportunity_record"].count == 1
    assert stages["human_execution"].count == 1  # 两次接管同一Opportunity
    assert stages["demand_signal"].count == 0  # 已知空来源不是未知
    assert stages["supply_match"].count is None
    assert result.qualified_opportunity_count is None
    assert "qualification_evidence_missing" in result.missing_inputs
    assert result.inputs.model_input_tokens is None
    assert result.inputs.human_work_seconds is None
    assert result.inputs.total_cost is None
    assert result.inputs.cost_per_qualified_opportunity is None
    assert "rate_card_missing" in result.inputs.missing_inputs
    assert result.handoffs.queue_depth == 1
    assert result.handoffs.oldest_wait_seconds == 3600
    assert result.handoffs.by_employee[0].employee_id == "emp_handler"
    assert "正文不可投影" not in result.model_dump_json()
    foreign = await repo.get_observability(other, **kwargs)
    assert {row.stage: row.count for row in foreign.stages}["opportunity_record"] == 0
    assert foreign.handoffs.queue_depth == 0
    assert foreign.handoffs.oldest_wait_seconds is None


async def test_future_handoff_clock_is_unknown_instead_of_zero(observation):
    repo, tenant, _, session = observation
    row = await session.get(HandoffRow, "hand_2")
    row.requested_at = NOW + timedelta(seconds=1)
    await session.flush()
    result = await repo.get_observability(
        tenant, start=NOW - timedelta(days=1), end=NOW, observed_at=NOW
    )
    assert result.handoffs.queue_depth == 1
    assert result.handoffs.oldest_wait_seconds is None
    assert result.handoffs.invalid_time_count == 1


async def test_service_enforces_scope_and_bounded_aware_window(observation):
    repo, tenant, other, _ = observation
    service = RunAuditService(repo, Phase1RunAuditAuthorizer(tenant), now=lambda: NOW)
    boss = RunAuditActor("emp_boss", "boss")
    for target, actor in [(other, boss), (tenant, RunAuditActor("emp_sales", "sales"))]:
        with pytest.raises(PermissionDenied):
            await service.get_observability(target, actor=actor)
    for start, end in [
        (NOW, None),
        (NOW.replace(tzinfo=None), NOW),
        (NOW - timedelta(days=32), NOW),
        (NOW, NOW),
        (NOW, NOW + timedelta(seconds=1)),
    ]:
        with pytest.raises(ValidationError):
            await service.get_observability(tenant, actor=boss, start=start, end=end)
    result = await service.get_observability(tenant, actor=boss)
    assert result.window_start == NOW - timedelta(days=7)
    assert result.window_end == NOW


async def test_run_binding_is_persisted_and_clock_anomaly_is_unknown(observation):
    from infra.db.tables import WorkflowRunRow, WorkflowStepRow
    from shared.schemas.identifiers import RunId

    repo, tenant, other, session = observation
    session.add(
        WorkflowRunRow(
            tenant_id=tenant,
            run_id="run_bound",
            workflow_type="human_handoff",
            workflow_version=1,
            subject_ref="hand_2",
            current_step="notify_owner",
            status="failed",
            created_at=NOW,
            context={"body": "正文不可投影"},
            idempotency_key="bound",
        )
    )
    session.add(
        WorkflowRunRow(
            tenant_id=tenant,
            run_id="run_unbound",
            workflow_type="unrelated",
            workflow_version=1,
            subject_ref="opp_window",
            current_step="work",
            status="failed",
            created_at=NOW,
            context={},
            idempotency_key="unbound",
        )
    )
    await session.flush()
    session.add(
        WorkflowStepRow(
            tenant_id=tenant,
            run_id="run_bound",
            step_id="wfs_bound",
            step_name="notify_owner",
            status="failed",
            attempt=2,
            due_at=NOW,
            data={},
            created_at=NOW,
            updated_at=NOW - timedelta(days=8),
            idempotency_key="bound-step",
        )
    )
    await session.flush()
    bound = await repo.get_run(tenant, RunId("run_bound"))
    assert bound.observation.handoff_id == "hand_2"
    assert bound.observation.opportunity_id == "opp_window"
    assert bound.observation.need_id == "need_0"
    assert bound.observation.responsible_employee_id == "emp_handler"
    assert bound.observation.recorded_span_seconds is None
    assert bound.observation.invalid_time_count == 1
    assert bound.observation.inputs.total_cost is None
    assert "正文不可投影" not in bound.model_dump_json()
    unbound = await repo.get_run(tenant, RunId("run_unbound"))
    assert unbound.observation.opportunity_id is None
    assert await repo.get_run(other, RunId("run_bound")) is None


async def test_gateway_replay_receipts_do_not_inflate_calls_or_cost(integration_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tests.integration.test_tool_gateway_pipeline import _Clock, _context, _gateway

    tenant = TenantId("tn_" + uuid4().hex[:24])
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    gateway, handler, _ = _gateway(factory, tenant, _Clock(NOW - timedelta(minutes=1)))
    await gateway.invoke(_context(tenant, "observation-replay"))
    await gateway.invoke(_context(tenant, "observation-replay"))
    repository = PostgresRunAuditRepository(factory)
    result = await repository.get_observability(
        tenant, start=NOW - timedelta(days=1), end=NOW, observed_at=NOW
    )
    assert handler.execute_calls == 1
    assert result.source_calls[0].call_count == 1
    assert result.source_calls[0].attempt_count == 1
    assert result.source_calls[0].duplicate_receipt_count == 1
    assert result.inputs.total_cost is None


async def test_credit_reservations_use_creation_window_and_current_status(
    integration_engine,
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from connectors.search_contracts import SearchCostStatus, SearchUsage
    from infra.db.search_quota import PostgresSearchQuotaRepository
    from shared.schemas.identifiers import RunId

    tenant = TenantId("tn_" + uuid4().hex[:24])
    other = TenantId("tn_" + uuid4().hex[:24])
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    clock = [NOW - timedelta(hours=1)]
    quota = PostgresSearchQuotaRepository(factory, tenant, now=lambda: clock[0])
    foreign = PostgresSearchQuotaRepository(factory, other, now=lambda: clock[0])
    usage = SearchUsage("Researcher", 10, 0, False, SearchCostStatus.FREE)
    for run, key in [
        ("run_consumed", "a"),
        ("run_reserved", "b"),
        ("run_uncertain", "c"),
    ]:
        await quota.reserve(RunId(run), key * 64, usage, fingerprint_version="v1")
    from tool_gateway.errors import ToolGatewayError

    with pytest.raises(ToolGatewayError):
        await foreign.reserve(
            RunId("run_foreign"), "a" * 64, usage, fingerprint_version="v1"
        )
    # 消耗发生在窗口end之后；所查是创建窗口内记录的当前状态。
    clock[0] = NOW
    await quota.mark_dispatched(RunId("run_consumed"), "a" * 64)
    await quota.consume(RunId("run_consumed"), "a" * 64)
    await quota.mark_dispatched(RunId("run_uncertain"), "c" * 64)
    result = await PostgresRunAuditRepository(factory).get_observability(
        tenant,
        start=NOW - timedelta(days=1),
        end=NOW - timedelta(minutes=30),
        observed_at=NOW,
    )
    assert (
        result.consumed_credits,
        result.reserved_credits,
        result.uncertain_credits,
    ) == (1, 1, 1)
    assert result.inputs.total_cost is None  # 免费额度状态不是可确认金额

    foreign_result = await PostgresRunAuditRepository(factory).get_observability(
        other, start=NOW - timedelta(days=1), end=NOW, observed_at=NOW,
    )
    assert (foreign_result.consumed_credits, foreign_result.reserved_credits, foreign_result.uncertain_credits) == (0, 0, 0)
