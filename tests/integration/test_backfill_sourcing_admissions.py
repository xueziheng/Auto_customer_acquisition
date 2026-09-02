"""历史寻源准入回填的 PostgreSQL 安全与竞态边界。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.service_impl import DemandServiceImpl
from domains.sourcing.permissions import Phase2SourcingAuthorizer
from domains.sourcing.schemas import (
    NeedFact,
    SourcingNeedSnapshot,
    canonical_sourcing_need_snapshot_hash,
)
from domains.sourcing.service_impl import SourcingServiceImpl
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import SourcingAdmissionRow
from infra.db.workflow_engine import PostgresWorkflowEngine
from infra.db.workflow_subject_lock import (
    acquire_workflow_subject_lock,
    workflow_subject_lock_identity,
)
from scripts.backfill_sourcing_admissions import (
    HistoricalSourcingCase,
    PostgresHistoricalCaseInventory,
    PostgresSourcingAdmissionWriter,
    _UnavailableCandidateEvidenceReader,
    execute_backfill,
    run_database_backfill,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, ValidatedNeedId
from shared.schemas.provenance import ProvenanceSummary, SourceType
from workflows.engine.runner import StepDefinition, WorkflowDefinition, WorkflowRun

TENANT = TenantId("tn_backfill_it_owner")
OTHER_TENANT = TenantId("tn_backfill_it_other")
TENANT_PREFIX = "tn_backfill_it_%"


@pytest_asyncio.fixture(autouse=True)
async def _clean_backfill_rows(
    integration_engine: AsyncEngine,
) -> AsyncIterator[None]:
    async def clean() -> None:
        async with integration_engine.begin() as connection:
            # append-only snapshot 的 DELETE trigger 也保护测试库；仅在本事务、本租户
            # 前缀的清理语句中关闭 trigger/FK，避免全表 TRUNCATE 污染并行节点。
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            for table in (
                "workflow_steps",
                "workflow_runs",
                "sourcing_priority_snapshots",
                "sourcing_admissions",
                "outbox_events",
                "sourcing_cases",
                "validated_needs",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id LIKE :prefix"),
                    {"prefix": TENANT_PREFIX},
                )

    await clean()
    try:
        yield
    finally:
        await clean()


def _provenance(observed_at: datetime) -> ProvenanceSummary:
    return ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-backfill-it",
        extracted_by="human",
        extracted_at=observed_at,
        confirmed_by=EmployeeId("emp-backfill-it"),
        confirmed_at=observed_at,
    )


def _snapshot(need_id: ValidatedNeedId, observed_at: datetime) -> SourcingNeedSnapshot:
    provenance = _provenance(observed_at)
    draft = SourcingNeedSnapshot(
        need_id=need_id,
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(value="hinges", provenance=provenance),
        application=NeedFact(value="marine doors", provenance=provenance),
        quantity=NeedFact(value=5000, provenance=provenance),
        snapshot_hash="0" * 64,
    )
    return draft.model_copy(
        update={"snapshot_hash": canonical_sourcing_need_snapshot_hash(draft)}
    )


async def _seed_case(
    engine: AsyncEngine,
    tenant_id: TenantId,
    suffix: str,
    *,
    state: str = "opened",
    malformed: bool = False,
) -> tuple[str, str, datetime]:
    need_id = ValidatedNeedId(f"need-{suffix}")
    case_id = f"src-{suffix}"
    opened_at = datetime.now(UTC) - timedelta(days=3)
    snapshot = _snapshot(need_id, opened_at - timedelta(days=1))
    stored_snapshot = snapshot.model_dump(mode="json")
    if malformed:
        stored_snapshot["product_category"]["value"] = "tampered"
    product_category = snapshot.product_category.model_dump(mode="json")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id,need_id,account_id,product_category,source_message_id,"
                "status,created_at) VALUES "
                "(:tenant,:need,:account,CAST(:category AS jsonb),:message,"
                "'sourcing_ready',:created_at)"
            ),
            {
                "tenant": str(tenant_id),
                "need": str(need_id),
                "account": f"acct-{suffix}",
                "category": json.dumps(product_category),
                "message": f"msg-{suffix}",
                "created_at": opened_at - timedelta(days=1),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO sourcing_cases "
                "(tenant_id,case_id,need_id,workflow_version,trigger_key,"
                "need_snapshot,need_snapshot_hash,state,sealed_candidate_ids,"
                "version,opened_at,state_changed_at,failed_reason) VALUES "
                "(:tenant,:case,:need,2,:trigger,CAST(:snapshot AS jsonb),:hash,"
                ":state,CAST('[]' AS jsonb),1,:opened_at,:opened_at,:failed_reason)"
            ),
            {
                "tenant": str(tenant_id),
                "case": case_id,
                "need": str(need_id),
                "trigger": f"backfill-it:{tenant_id}:{suffix}",
                "snapshot": json.dumps(stored_snapshot),
                "hash": snapshot.snapshot_hash,
                "state": state,
                "opened_at": opened_at,
                "failed_reason": "controlled-test-terminal" if state == "failed" else None,
            },
        )
    return case_id, str(need_id), opened_at


async def _admission_count(engine: AsyncEngine, tenant_id: TenantId) -> int:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(SourcingAdmissionRow)
                .where(SourcingAdmissionRow.tenant_id == str(tenant_id))
            )
            or 0
        )


async def _seed_legacy_run_without_admission(
    engine: AsyncEngine,
    tenant_id: TenantId,
    case_id: str,
) -> None:
    """构造迁移前可能存在的 Run；新写入口不得再产生这种状态。"""

    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL session_replication_role = replica"))
        await connection.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id,tenant_id,workflow_type,workflow_version,subject_ref,"
                "current_step,status,context,idempotency_key) VALUES "
                "(:run,:tenant,'sourcing_case',2,:case,'first','running',"
                "CAST('{}' AS jsonb),:key)"
            ),
            {
                "run": f"run_{case_id}",
                "tenant": str(tenant_id),
                "case": case_id,
                "key": f"legacy:{tenant_id}:{case_id}",
            },
        )


async def _mark_admission_starting(
    engine: AsyncEngine,
    tenant_id: TenantId,
    case_id: str,
) -> None:
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sourcing_admissions SET state = 'starting', "
                "claim_token = 'controlled-test-claim', "
                "claim_expires_at = :expires, updated_at = :now "
                "WHERE tenant_id = :tenant AND case_id = :case"
            ),
            {
                "tenant": str(tenant_id),
                "case": case_id,
                "now": now,
                "expires": now + timedelta(minutes=5),
            },
        )


_RELATED_TABLES = (
    "validated_needs",
    "sourcing_cases",
    "sourcing_admissions",
    "sourcing_priority_snapshots",
    "workflow_runs",
    "workflow_steps",
    "outbox_events",
)


async def _related_counts(
    engine: AsyncEngine,
    tenant_id: TenantId,
) -> dict[str, int]:
    async with engine.connect() as connection:
        return {
            table: int(
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tenant"),
                    {"tenant": str(tenant_id)},
                )
                or 0
            )
            for table in _RELATED_TABLES
        }


class _NoopHandler:
    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, object]]:
        del run
        return "complete", None, {}


def _workflow_engine(
    factory: async_sessionmaker[AsyncSession],
) -> PostgresWorkflowEngine:
    engine = PostgresWorkflowEngine(factory, {"noop": _NoopHandler()})
    engine.register(
        WorkflowDefinition(
            workflow_type="sourcing_case",
            version=2,
            steps=(StepDefinition(step_name="first", handler_ref="noop"),),
            transitions={},
        )
    )
    return engine


@pytest.mark.asyncio
async def test_database_dry_run_is_exact_tenant_and_zero_write(
    integration_engine: AsyncEngine,
    db_url: str,
) -> None:
    """dry-run 只能报告 owner tenant，且对 owner/other 均零 Admission 写入。"""

    owner_case, _, _ = await _seed_case(integration_engine, TENANT, "dry-owner")
    await _seed_case(integration_engine, OTHER_TENANT, "dry-other")
    owner_before = await _related_counts(integration_engine, TENANT)
    other_before = await _related_counts(integration_engine, OTHER_TENANT)

    report = await run_database_backfill(db_url, TENANT, False)

    assert report.to_dict()["results"] == [
        {"case_id": owner_case, "status": "would_create", "reason": "eligible"}
    ]
    assert await _related_counts(integration_engine, TENANT) == owner_before
    assert await _related_counts(integration_engine, OTHER_TENANT) == other_before


@pytest.mark.asyncio
async def test_database_apply_filters_states_and_replay_is_idempotent(
    integration_engine: AsyncEngine,
    db_url: str,
) -> None:
    """真实 UoW 只补合格行；started/terminal/malformed/Run 固定跳过且重跑幂等。"""

    eligible_case, _, opened_at = await _seed_case(
        integration_engine, TENANT, "apply-owner"
    )
    started_case, _, _ = await _seed_case(
        integration_engine, TENANT, "apply-started", state="discovering"
    )
    terminal_case, _, _ = await _seed_case(
        integration_engine, TENANT, "apply-terminal", state="failed"
    )
    malformed_case, _, _ = await _seed_case(
        integration_engine, TENANT, "apply-malformed", malformed=True
    )
    run_case, _, _ = await _seed_case(integration_engine, TENANT, "apply-run")
    await _seed_case(integration_engine, OTHER_TENANT, "apply-other")
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    await _seed_legacy_run_without_admission(integration_engine, TENANT, run_case)

    first = await run_database_backfill(db_url, TENANT, True)
    second = await run_database_backfill(db_url, TENANT, True)

    first_by_case = {item.case_id: (item.status, item.reason) for item in first.results}
    assert first_by_case == {
        eligible_case: ("applied", "admission_ensured"),
        malformed_case: ("skipped", "malformed_need_snapshot"),
        run_case: ("skipped", "workflow_run_exists"),
        started_case: ("skipped", "case_already_started"),
        terminal_case: ("skipped", "case_terminal"),
    }
    second_by_case = {item.case_id: (item.status, item.reason) for item in second.results}
    assert second_by_case[eligible_case] == ("skipped", "already_admitted")
    assert await _admission_count(integration_engine, TENANT) == 1
    assert await _admission_count(integration_engine, OTHER_TENANT) == 0
    async with factory() as session:
        ready_at = await session.scalar(
            select(SourcingAdmissionRow.ready_at).where(
                SourcingAdmissionRow.tenant_id == str(TENANT),
                SourcingAdmissionRow.case_id == eligible_case,
            )
        )
    assert ready_at == opened_at


class _CapturedInventory:
    def __init__(self, rows: list[HistoricalSourcingCase]) -> None:
        self._rows = rows

    async def list_v2_cases(
        self, tenant_id: TenantId
    ) -> list[HistoricalSourcingCase]:
        del tenant_id
        return list(self._rows)


@pytest.mark.asyncio
async def test_claimed_run_wins_shared_lock_and_backfill_fails_closed(
    integration_engine: AsyncEngine,
) -> None:
    """正常 engine 与回填写入口共享锁；Run 先排队时回填复核并固定跳过。"""

    case_id, _, _ = await _seed_case(integration_engine, TENANT, "race")
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    inventory = PostgresHistoricalCaseInventory(factory, TENANT)
    stale_rows = await inventory.list_v2_cases(TENANT)
    assert stale_rows[0].has_workflow_run is False
    clock = lambda: datetime.now(UTC)
    demand = DemandServiceImpl(
        lambda tenant: SqlAlchemyDemandUnitOfWork(factory, tenant, now=clock),
        now=clock,
    )
    service = SourcingServiceImpl(
        lambda tenant: SqlAlchemySourcingUnitOfWork(factory, tenant),
        Phase2SourcingAuthorizer(TENANT),
        _UnavailableCandidateEvidenceReader(),
        now=clock,
    )
    writer = PostgresSourcingAdmissionWriter(factory, service)
    initial = await execute_backfill(
        tenant_id=TENANT,
        apply=True,
        inventory=_CapturedInventory(stale_rows),
        demand=demand,
        sourcing=writer,
        now=clock,
    )
    assert initial.results[0].status == "applied"
    await _mark_admission_starting(integration_engine, TENANT, case_id)
    workflow = _workflow_engine(factory)
    async with factory() as guard, guard.begin():
        await acquire_workflow_subject_lock(
            guard, TENANT, "sourcing_case", case_id
        )
        run_task = asyncio.create_task(
            workflow.start(
                TENANT,
                "sourcing_case",
                case_id,
                {},
                "backfill-it-racing-run",
            )
        )
        await asyncio.sleep(0.05)
        assert not run_task.done(), "正常 Run 创建必须等待同一 subject 锁"
        backfill_task = asyncio.create_task(
            execute_backfill(
                tenant_id=TENANT,
                apply=True,
                inventory=_CapturedInventory(stale_rows),
                demand=demand,
                sourcing=writer,
                now=clock,
            )
        )
        await asyncio.sleep(0.05)
        assert not backfill_task.done(), "回填写入口必须等待同一 subject 锁"
    await run_task
    report = await backfill_task

    assert report.to_dict()["results"] == [
        {
            "case_id": case_id,
            "status": "skipped",
            "reason": "workflow_run_exists",
        }
    ]
    assert await _admission_count(integration_engine, TENANT) == 1


@pytest.mark.asyncio
async def test_snapshot_changed_after_inventory_is_rejected_under_case_lock(
    integration_engine: AsyncEngine,
) -> None:
    """库存读取后快照漂移时，生产领域服务必须在 Case 行锁内拒绝准入。"""

    case_id, _, _ = await _seed_case(integration_engine, TENANT, "snapshot-race")
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    inventory = PostgresHistoricalCaseInventory(factory, TENANT)
    stale_rows = await inventory.list_v2_cases(TENANT)
    async with integration_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sourcing_cases SET need_snapshot = "
                "jsonb_set(need_snapshot, '{product_category,value}', "
                "CAST(:value AS jsonb)) "
                "WHERE tenant_id = :tenant AND case_id = :case"
            ),
            {
                "tenant": str(TENANT),
                "case": case_id,
                "value": json.dumps("tampered"),
            },
        )
    clock = lambda: datetime.now(UTC)
    demand = DemandServiceImpl(
        lambda tenant: SqlAlchemyDemandUnitOfWork(factory, tenant, now=clock),
        now=clock,
    )
    service = SourcingServiceImpl(
        lambda tenant: SqlAlchemySourcingUnitOfWork(factory, tenant),
        Phase2SourcingAuthorizer(TENANT),
        _UnavailableCandidateEvidenceReader(),
        now=clock,
    )

    report = await execute_backfill(
        tenant_id=TENANT,
        apply=True,
        inventory=_CapturedInventory(stale_rows),
        demand=demand,
        sourcing=PostgresSourcingAdmissionWriter(factory, service),
        now=clock,
    )

    assert report.to_dict()["results"] == [
        {
            "case_id": case_id,
            "status": "skipped",
            "reason": "eligibility_changed",
        }
    ]
    assert await _admission_count(integration_engine, TENANT) == 0


@pytest.mark.asyncio
async def test_backfill_wins_then_waiting_engine_start_is_rejected(
    integration_engine: AsyncEngine,
) -> None:
    """start 已排队也不能在回填提交 WAITING 后创建 V2 Run。"""

    case_id, _, _ = await _seed_case(integration_engine, TENANT, "backfill-wins")
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    inventory = PostgresHistoricalCaseInventory(factory, TENANT)
    stale_rows = await inventory.list_v2_cases(TENANT)
    clock = lambda: datetime.now(UTC)
    demand = DemandServiceImpl(
        lambda tenant: SqlAlchemyDemandUnitOfWork(factory, tenant, now=clock),
        now=clock,
    )
    service = SourcingServiceImpl(
        lambda tenant: SqlAlchemySourcingUnitOfWork(factory, tenant),
        Phase2SourcingAuthorizer(TENANT),
        _UnavailableCandidateEvidenceReader(),
        now=clock,
    )
    writer = PostgresSourcingAdmissionWriter(factory, service)
    workflow = _workflow_engine(factory)

    async with factory() as guard, guard.begin():
        await acquire_workflow_subject_lock(guard, TENANT, "sourcing_case", case_id)
        backfill_task = asyncio.create_task(
            execute_backfill(
                tenant_id=TENANT,
                apply=True,
                inventory=_CapturedInventory(stale_rows),
                demand=demand,
                sourcing=writer,
                now=clock,
            )
        )
        await asyncio.sleep(0.05)
        start_task = asyncio.create_task(
            workflow.start(
                TENANT,
                "sourcing_case",
                case_id,
                {},
                "backfill-it-waiting-start",
            )
        )
        await asyncio.sleep(0.05)
        assert not backfill_task.done()
        assert not start_task.done()

    report = await backfill_task
    assert report.to_dict()["results"] == [
        {
            "case_id": case_id,
            "status": "applied",
            "reason": "admission_ensured",
        }
    ]
    with pytest.raises(ValidationError, match="准入状态不允许启动"):
        await start_task

    async with factory() as session:
        run_count = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM workflow_runs "
                    "WHERE tenant_id = :tenant AND workflow_type = 'sourcing_case' "
                    "AND subject_ref = :case"
                ),
                {"tenant": str(TENANT), "case": case_id},
            )
            or 0
        )
        state = await session.scalar(
            select(SourcingAdmissionRow.state).where(
                SourcingAdmissionRow.tenant_id == str(TENANT),
                SourcingAdmissionRow.case_id == case_id,
            )
        )
    assert run_count == 0
    assert state == "waiting"


@pytest.mark.asyncio
async def test_subject_lock_identity_isolated_and_released_after_cancel(
    integration_engine: AsyncEngine,
) -> None:
    """歧义 tuple 不共锁；等待取消与 rollback 后连接池和锁均释放。"""

    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    first_tuple = (TenantId("tn_backfill_it_lock:a"), "b", "c")
    second_tuple = (TenantId("tn_backfill_it_lock"), "a:b", "c")
    assert workflow_subject_lock_identity(*first_tuple) != workflow_subject_lock_identity(
        *second_tuple
    )

    async with factory() as holder:
        await holder.begin()
        await acquire_workflow_subject_lock(holder, *first_tuple)
        async with factory() as isolated, isolated.begin():
            await isolated.execute(text("SET LOCAL lock_timeout = '200ms'"))
            await acquire_workflow_subject_lock(isolated, *second_tuple)

        waiter = factory()
        await waiter.begin()
        waiting = asyncio.create_task(
            acquire_workflow_subject_lock(waiter, *first_tuple)
        )
        await asyncio.sleep(0.05)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await waiter.rollback()
        await waiter.close()
        await holder.rollback()

    async with factory() as probe, probe.begin():
        await probe.execute(text("SET LOCAL lock_timeout = '200ms'"))
        await acquire_workflow_subject_lock(probe, *first_tuple)

    checked_out = getattr(integration_engine.pool, "checkedout", None)
    if callable(checked_out):
        assert checked_out() == 0


@pytest.mark.asyncio
async def test_starting_admission_allows_start_and_unbound_recovery_replay(
    integration_engine: AsyncEngine,
    db_url: str,
) -> None:
    """STARTING 可首次创建；绑定前同键恢复仍返回 canonical Run。"""

    case_id, _, _ = await _seed_case(integration_engine, TENANT, "start-replay")
    report = await run_database_backfill(db_url, TENANT, True)
    assert report.results[0].status == "applied"
    await _mark_admission_starting(integration_engine, TENANT, case_id)
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    workflow = _workflow_engine(factory)
    key = "backfill-it-start-replay"

    run_id = await workflow.start(TENANT, "sourcing_case", case_id, {}, key)
    replayed = await workflow.start(TENANT, "sourcing_case", case_id, {}, key)

    assert replayed == run_id
    async with integration_engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM workflow_runs "
                    "WHERE tenant_id = :tenant AND subject_ref = :case"
                ),
                {"tenant": str(TENANT), "case": case_id},
            )
            == 1
        )


@pytest.mark.asyncio
async def test_blocked_admission_cannot_start_sourcing_v2_run(
    integration_engine: AsyncEngine,
    db_url: str,
) -> None:
    """BLOCKED 与 WAITING 一样不是 engine.start 的允许状态。"""

    case_id, _, _ = await _seed_case(integration_engine, TENANT, "blocked-start")
    report = await run_database_backfill(db_url, TENANT, True)
    assert report.results[0].status == "applied"
    async with integration_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sourcing_admissions SET state = 'blocked', "
                "blocked_reason = 'case_state_mismatch', updated_at = now() "
                "WHERE tenant_id = :tenant AND case_id = :case"
            ),
            {"tenant": str(TENANT), "case": case_id},
        )
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)

    with pytest.raises(ValidationError, match="准入状态不允许启动"):
        await _workflow_engine(factory).start(
            TENANT,
            "sourcing_case",
            case_id,
            {},
            "backfill-it-blocked-start",
        )
