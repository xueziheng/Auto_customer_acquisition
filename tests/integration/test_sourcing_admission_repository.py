"""PostgreSQL 寻源准入仓储：原子首快照、排序 claim 与租户隔离。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.sourcing.admission import (
    ADMISSION_RANKING_VERSION,
    AdmissionBlockedReason,
    AdmissionState,
    SourcingAdmission,
    SourcingPrioritySnapshot,
    canonical_priority_facts_hash,
)
from infra.db.repositories.sourcing import SourcingAdmissionRepositoryImpl
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import SourcingAdmissionRow, SourcingPrioritySnapshotRow
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    NeedClusterId,
    RunId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPrioritySnapshotId,
    TenantId,
    ValidatedNeedId,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
READY_AT = NOW - timedelta(days=2)


@pytest_asyncio.fixture(autouse=True)
async def _clean_admission_rows(integration_engine: AsyncEngine):
    async with integration_engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE sourcing_admissions, sourcing_priority_snapshots CASCADE")
        )
    yield
    async with integration_engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE sourcing_admissions, sourcing_priority_snapshots CASCADE")
        )
        await connection.execute(
            text("DELETE FROM sourcing_cases WHERE tenant_id LIKE 'tn_admission_%'")
        )
        await connection.execute(
            text("DELETE FROM validated_needs WHERE tenant_id LIKE 'tn_admission_%'")
        )


def _sessions(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_case(
    engine: AsyncEngine,
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    need_id: ValidatedNeedId,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id,need_id,account_id,product_category,source_message_id,"
                "status,created_at) VALUES "
                "(:tenant,:need,'account-a',CAST(:category AS jsonb),'message-a',"
                "'sourcing_ready',:now)"
            ),
            {
                "tenant": tenant_id,
                "need": need_id,
                "category": json.dumps({"value": "hinges"}),
                "now": NOW,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO sourcing_cases "
                "(tenant_id,case_id,need_id,workflow_version,trigger_key,"
                "need_snapshot,need_snapshot_hash,state,version,opened_at,"
                "state_changed_at) VALUES "
                "(:tenant,:case,:need,2,:trigger,CAST(:snapshot AS jsonb),"
                ":hash,'opened',1,:now,:now)"
            ),
            {
                "tenant": tenant_id,
                "case": case_id,
                "need": need_id,
                "trigger": f"{need_id}:admission-v1",
                "snapshot": json.dumps({"need_id": need_id, "completeness": 3}),
                "hash": "a" * 64,
                "now": NOW,
            },
        )


def _bundle(
    tenant: TenantId,
    suffix: str,
    count: int,
    *,
    state: AdmissionState = AdmissionState.WAITING,
    blocked_reason: AdmissionBlockedReason | None = None,
    snapshot_id: str | None = None,
    created_at: datetime = NOW,
    ready_at: datetime = READY_AT,
) -> tuple[SourcingAdmission, SourcingPrioritySnapshot]:
    admission_id = SourcingAdmissionId(f"adm_{suffix}")
    case_id = SourcingCaseId(f"case_{suffix}")
    need_id = ValidatedNeedId(f"need_{suffix}")
    current_snapshot_id = SourcingPrioritySnapshotId(snapshot_id or f"sps_{suffix}")
    cluster_id = NeedClusterId(f"ncl_{suffix}") if count > 1 else None
    facts_hash = canonical_priority_facts_hash(
        need_id=need_id,
        cluster_id=cluster_id,
        cluster_member_count=count,
        ready_at=ready_at,
        facts_observed_at=created_at,
        ranking_version=ADMISSION_RANKING_VERSION,
    )
    admission = SourcingAdmission(
        tenant_id=tenant,
        admission_id=admission_id,
        case_id=case_id,
        need_id=need_id,
        state=state,
        ready_at=ready_at,
        current_snapshot_id=current_snapshot_id,
        claim_token=None,
        claim_expires_at=None,
        workflow_run_id=None,
        blocked_reason=blocked_reason,
        admitted_at=None,
        admitted_by=None,
        created_at=NOW,
        updated_at=NOW,
    )
    snapshot = SourcingPrioritySnapshot(
        tenant_id=tenant,
        snapshot_id=current_snapshot_id,
        admission_id=admission_id,
        case_id=case_id,
        need_id=need_id,
        cluster_id=cluster_id,
        cluster_member_count=count,
        ready_at=ready_at,
        ranking_version=ADMISSION_RANKING_VERSION,
        facts_observed_at=created_at,
        facts_hash=facts_hash,
        created_at=created_at,
    )
    return admission, snapshot


class _ExplodingSession:
    async def execute(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("参数校验之后不应访问数据库")


def _exploding_repository(tenant: TenantId) -> SourcingAdmissionRepositoryImpl:
    return SourcingAdmissionRepositoryImpl(
        cast(AsyncSession, cast(object, _ExplodingSession())), tenant
    )


async def _store_bundle(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    admission: SourcingAdmission,
    snapshot: SourcingPrioritySnapshot | None,
) -> tuple[SourcingAdmission, SourcingPrioritySnapshot | None, bool]:
    await _seed_case(engine, admission.tenant_id, admission.case_id, admission.need_id)
    async with SqlAlchemySourcingUnitOfWork(sessions, admission.tenant_id) as uow:
        return await uow.admissions.get_or_create(
            admission.tenant_id, admission, snapshot
        )


async def test_get_or_create_atomically_binds_initial_snapshot_and_deduplicates(
    integration_engine: AsyncEngine,
) -> None:
    """若遗漏首快照插入或 pointer 绑定，canonical 重放会返回不完整记录。"""
    tenant = TenantId("tn_admission_atomic")
    admission, snapshot = _bundle(tenant, "atomic", 3)
    sessions = _sessions(integration_engine)

    stored, stored_snapshot, created = await _store_bundle(
        integration_engine, sessions, admission, snapshot
    )
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        (
            replayed,
            replayed_snapshot,
            replay_created,
        ) = await uow.admissions.get_or_create(tenant, admission, snapshot)

    assert created is True
    assert stored == admission
    assert stored_snapshot == snapshot
    assert replay_created is False
    assert replayed == admission
    assert replayed_snapshot == snapshot


async def test_current_snapshot_reads_are_tenant_bound_and_keep_repository_order(
    integration_engine: AsyncEngine,
) -> None:
    """安全 service 读取必须一次 JOIN 得到 current snapshot，且不改变 SQL 排序。"""

    tenant = TenantId("tn_admission_read_bundle")
    other_tenant = TenantId("tn_admission_read_other")
    sessions = _sessions(integration_engine)
    count_three, snapshot_three = _bundle(tenant, "read_three", 3)
    count_eight, snapshot_eight = _bundle(
        tenant, "read_eight", 8, ready_at=READY_AT + timedelta(hours=1)
    )
    invalid, _ = _bundle(tenant, "read_invalid", 1)
    invalid = replace(
        invalid,
        state=AdmissionState.BLOCKED,
        current_snapshot_id=None,
        blocked_reason=AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
    )
    for admission, snapshot in (
        (count_three, snapshot_three),
        (count_eight, snapshot_eight),
        (invalid, None),
    ):
        await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        owner = await uow.admissions.get_with_current_snapshot(
            tenant, count_three.admission_id
        )
        foreign = await uow.admissions.get_with_current_snapshot(
            tenant, SourcingAdmissionId("adm_not_owned")
        )
        waiting = await uow.admissions.list_by_state_with_current_snapshot(
            tenant, AdmissionState.WAITING, 20
        )
        blocked = await uow.admissions.list_by_state_with_current_snapshot(
            tenant, AdmissionState.BLOCKED, 20
        )
    async with SqlAlchemySourcingUnitOfWork(sessions, other_tenant) as other_uow:
        cross_tenant = await other_uow.admissions.get_with_current_snapshot(
            other_tenant, count_three.admission_id
        )

    assert owner == (count_three, snapshot_three)
    assert foreign is None
    assert cross_tenant is None
    assert [item[0].admission_id for item in waiting] == [
        count_eight.admission_id,
        count_three.admission_id,
    ]
    assert [item[1] for item in waiting] == [snapshot_eight, snapshot_three]
    assert blocked == [(invalid, None)]


async def test_get_or_create_is_atomic_under_concurrent_canonical_proposals(
    integration_engine: AsyncEngine,
) -> None:
    """若仅先查再插，同一 Need 的两个事务会重复建 admission。"""
    tenant = TenantId("tn_admission_create_race")
    first, first_snapshot = _bundle(tenant, "create_race", 3)
    second = replace(first, admission_id=SourcingAdmissionId("adm_create_other"))
    second_snapshot = replace(
        first_snapshot,
        snapshot_id=SourcingPrioritySnapshotId("sps_create_other"),
        admission_id=second.admission_id,
    )
    second = replace(second, current_snapshot_id=second_snapshot.snapshot_id)
    await _seed_case(integration_engine, tenant, first.case_id, first.need_id)
    sessions = _sessions(integration_engine)

    async def create_one(
        admission: SourcingAdmission, snapshot: SourcingPrioritySnapshot
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot | None, bool]:
        async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
            return await uow.admissions.get_or_create(tenant, admission, snapshot)

    results = await asyncio.gather(
        create_one(first, first_snapshot), create_one(second, second_snapshot)
    )

    assert sorted(result[2] for result in results) == [False, True]
    assert len({result[0].admission_id for result in results}) == 1
    assert len({result[1].snapshot_id for result in results if result[1]}) == 1
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(SourcingAdmissionRow))
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(SourcingPrioritySnapshotRow)
            )
            == 1
        )


async def test_append_snapshot_deduplicates_and_recovers_only_priority_invalid(
    integration_engine: AsyncEngine,
) -> None:
    """新快照应修复 facts-invalid，但不得误恢复 Case 状态不匹配。"""
    sessions = _sessions(integration_engine)
    tenant = TenantId("tn_admission_append")

    invalid, repaired_snapshot = _bundle(tenant, "invalid", 1)
    invalid = replace(
        invalid,
        state=AdmissionState.BLOCKED,
        current_snapshot_id=None,
        blocked_reason=AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
    )
    await _store_bundle(integration_engine, sessions, invalid, None)
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        repaired, canonical, created = await uow.admissions.append_snapshot_if_changed(
            tenant, repaired_snapshot
        )
    replay = replace(
        repaired_snapshot,
        snapshot_id=SourcingPrioritySnapshotId("sps_invalid_replay"),
        created_at=NOW + timedelta(minutes=1),
    )
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        (
            unchanged,
            replay_canonical,
            replay_created,
        ) = await uow.admissions.append_snapshot_if_changed(tenant, replay)

    assert repaired.state is AdmissionState.WAITING
    assert repaired.blocked_reason is None
    assert repaired.current_snapshot_id == canonical.snapshot_id
    assert created is True
    assert replay_created is False
    assert replay_canonical.snapshot_id == canonical.snapshot_id
    assert unchanged == repaired

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        blocked_with_snapshot = await uow.admissions.block(
            tenant,
            invalid.admission_id,
            AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
            NOW + timedelta(minutes=1),
        )
    assert blocked_with_snapshot is not None
    same_hash_repair = replace(
        repaired_snapshot,
        snapshot_id=SourcingPrioritySnapshotId("sps_invalid_same_hash_repair"),
        created_at=NOW + timedelta(minutes=2),
    )
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        (
            same_hash_recovered,
            same_hash_canonical,
            same_hash_created,
        ) = await uow.admissions.append_snapshot_if_changed(tenant, same_hash_repair)
    assert same_hash_recovered.state is AdmissionState.WAITING
    assert same_hash_recovered.blocked_reason is None
    assert same_hash_canonical.snapshot_id == canonical.snapshot_id
    assert same_hash_created is False

    mismatch, mismatch_snapshot = _bundle(
        tenant,
        "mismatch",
        3,
        state=AdmissionState.BLOCKED,
        blocked_reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
    )
    await _store_bundle(integration_engine, sessions, mismatch, mismatch_snapshot)
    changed_snapshot = replace(
        mismatch_snapshot,
        snapshot_id=SourcingPrioritySnapshotId("sps_mismatch_changed"),
        cluster_member_count=4,
        facts_observed_at=NOW + timedelta(minutes=2),
        created_at=NOW + timedelta(minutes=2),
        facts_hash=canonical_priority_facts_hash(
            need_id=mismatch.need_id,
            cluster_id=mismatch_snapshot.cluster_id,
            cluster_member_count=4,
            ready_at=READY_AT,
            facts_observed_at=NOW + timedelta(minutes=2),
            ranking_version=ADMISSION_RANKING_VERSION,
        ),
    )
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        still_blocked, _, _ = await uow.admissions.append_snapshot_if_changed(
            tenant, changed_snapshot
        )
    assert still_blocked.state is AdmissionState.BLOCKED
    assert still_blocked.blocked_reason is AdmissionBlockedReason.CASE_STATE_MISMATCH


async def test_append_rejects_backward_snapshot_without_persisting_it(
    integration_engine: AsyncEngine,
) -> None:
    """早于 admission.updated_at 的合法快照对象不得留下孤立证据或倒拨 pointer。"""
    tenant = TenantId("tn_admission_append_monotonic")
    admission, snapshot = _bundle(tenant, "append_monotonic", 1)
    sessions = _sessions(integration_engine)
    await _store_bundle(integration_engine, sessions, admission, snapshot)
    backward_at = NOW - timedelta(minutes=1)
    backward = replace(
        snapshot,
        snapshot_id=SourcingPrioritySnapshotId("sps_append_backward"),
        facts_observed_at=backward_at,
        facts_hash=canonical_priority_facts_hash(
            need_id=admission.need_id,
            cluster_id=None,
            cluster_member_count=1,
            ready_at=admission.ready_at,
            facts_observed_at=backward_at,
            ranking_version=ADMISSION_RANKING_VERSION,
        ),
        created_at=backward_at,
    )

    with pytest.raises(ValidationError, match="snapshot.created_at"):
        async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
            await uow.admissions.append_snapshot_if_changed(tenant, backward)

    async with sessions() as session:
        stored = await session.scalar(
            select(SourcingAdmissionRow).where(
                SourcingAdmissionRow.tenant_id == str(tenant),
                SourcingAdmissionRow.admission_id == str(admission.admission_id),
            )
        )
        snapshot_count = await session.scalar(
            select(func.count())
            .select_from(SourcingPrioritySnapshotRow)
            .where(SourcingPrioritySnapshotRow.tenant_id == str(tenant))
        )
    assert stored is not None
    assert stored.current_snapshot_id == str(snapshot.snapshot_id)
    assert stored.updated_at == admission.updated_at
    assert snapshot_count == 1


async def test_claim_ordered_uses_database_priority_and_claims_once_concurrently(
    integration_engine: AsyncEngine,
) -> None:
    """错误排序或缺 SKIP LOCKED 会返回 1→8，或让同一行被 claim 两次。"""
    tenant = TenantId("tn_admission_claim")
    sessions = _sessions(integration_engine)
    bundles = [_bundle(tenant, f"count_{count}", count) for count in (1, 8, 3)]
    for admission, snapshot in bundles:
        await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        claimed = await uow.admissions.claim_ordered(
            tenant,
            limit=2,
            claim_token="claim-priority",
            claim_expires_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(minutes=1),
        )
    assert [item.need_id for item in claimed] == [
        ValidatedNeedId("need_count_8"),
        ValidatedNeedId("need_count_3"),
    ]

    lone_tenant = TenantId("tn_admission_claim_once")
    lone, lone_snapshot = _bundle(lone_tenant, "claim_once", 1)
    await _store_bundle(integration_engine, sessions, lone, lone_snapshot)

    async def claim_one(token: str) -> list[SourcingAdmission]:
        async with SqlAlchemySourcingUnitOfWork(sessions, lone_tenant) as uow:
            return await uow.admissions.claim_ordered(
                lone_tenant,
                limit=1,
                claim_token=token,
                claim_expires_at=NOW + timedelta(minutes=5),
                now=NOW + timedelta(minutes=1),
            )

    concurrent = await asyncio.gather(claim_one("claim-a"), claim_one("claim-b"))
    assert sorted(len(items) for items in concurrent) == [0, 1]
    assert (
        sum(
            item.admission_id == lone.admission_id
            for items in concurrent
            for item in items
        )
        == 1
    )


async def test_claim_one_targets_exact_row_and_replays_same_token(
    integration_engine: AsyncEngine,
) -> None:
    """人工准入不能借全局排序误 claim 队首，原请求重放返回同一 starting。"""

    tenant = TenantId("tn_admission_manual_claim")
    sessions = _sessions(integration_engine)
    high, high_snapshot = _bundle(tenant, "manual_high", 8)
    target, target_snapshot = _bundle(tenant, "manual_target", 1)
    await _store_bundle(integration_engine, sessions, high, high_snapshot)
    await _store_bundle(integration_engine, sessions, target, target_snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        claimed = await uow.admissions.claim_one(
            tenant,
            target.admission_id,
            "manual-request-1",
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=1),
            requested_by="employee-boss",
        )
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        replayed = await uow.admissions.claim_one(
            tenant,
            target.admission_id,
            "manual-request-1",
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=1),
            requested_by="employee-boss",
        )
        untouched = await uow.admissions.get(tenant, high.admission_id)

    assert claimed == replayed
    assert claimed is not None
    assert claimed.admission_id == target.admission_id
    assert claimed.state is AdmissionState.STARTING
    assert claimed.claim_token == "manual-request-1"
    assert untouched is not None and untouched.state is AdmissionState.WAITING


async def test_manual_request_actor_survives_release_reclaim_and_drives_complete(
    integration_engine: AsyncEngine,
) -> None:
    """真实 PG 必须跨事务/租约保存人工 actor，complete 不能接受伪造 admitted_by。"""

    tenant = TenantId("tn_admission_durable_actor")
    sessions = _sessions(integration_engine)
    admission, snapshot = _bundle(tenant, "durable_actor", 1)
    await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        manual = await uow.admissions.claim_one(
            tenant,
            admission.admission_id,
            "manual-request-durable",
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=1),
            requested_by="employee-boss",
        )
    assert manual is not None
    assert manual.admission_requested_by == "employee-boss"
    assert manual.manual_request_id == "manual-request-durable"

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        released = await uow.admissions.release_expired_claims(
            tenant, NOW + timedelta(minutes=3)
        )
    assert released[0].admission_requested_by == "employee-boss"
    assert released[0].manual_request_id == "manual-request-durable"

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        wrong_key = await uow.admissions.claim_one(
            tenant,
            admission.admission_id,
            "manual-request-new",
            NOW + timedelta(minutes=8),
            NOW + timedelta(minutes=3),
            requested_by="employee-boss",
        )
        wrong_actor = await uow.admissions.claim_one(
            tenant,
            admission.admission_id,
            "manual-request-durable",
            NOW + timedelta(minutes=8),
            NOW + timedelta(minutes=3),
            requested_by="employee-other",
        )

    assert wrong_key is not None and wrong_key.state is AdmissionState.WAITING
    assert wrong_actor is not None and wrong_actor.state is AdmissionState.WAITING
    assert wrong_key.manual_request_id == "manual-request-durable"
    assert wrong_actor.admission_requested_by == "employee-boss"

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        reclaimed = await uow.admissions.claim_ordered(
            tenant,
            1,
            "scheduler-recovery",
            NOW + timedelta(minutes=8),
            NOW + timedelta(minutes=3),
        )
        completed = await uow.admissions.complete(
            tenant,
            admission.admission_id,
            "scheduler-recovery",
            RunId("run-durable-actor"),
            "system:sourcing-admission",
            NOW + timedelta(minutes=4),
        )

    assert reclaimed[0].admission_requested_by == "employee-boss"
    assert reclaimed[0].manual_request_id == "manual-request-durable"
    assert reclaimed[0].claim_token == "scheduler-recovery"
    assert completed is not None
    assert completed.admitted_by == "employee-boss"
    assert completed.admission_requested_by is None
    assert completed.manual_request_id is None


async def test_manual_claim_does_not_overwrite_other_tenant_or_existing_actor(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_admission_actor_owner")
    other = TenantId("tn_admission_actor_other")
    sessions = _sessions(integration_engine)
    admission, snapshot = _bundle(tenant, "actor_owner", 1)
    await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        first = await uow.admissions.claim_one(
            tenant,
            admission.admission_id,
            "same-request",
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=1),
            requested_by="employee-boss",
        )
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        replay = await uow.admissions.claim_one(
            tenant,
            admission.admission_id,
            "same-request",
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=1),
            requested_by="employee-sourcing",
        )
    async with SqlAlchemySourcingUnitOfWork(sessions, other) as uow:
        cross_tenant = await uow.admissions.claim_one(
            other,
            admission.admission_id,
            "same-request",
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=1),
            requested_by="employee-other",
        )

    assert first is not None and first.admission_requested_by == "employee-boss"
    assert replay is not None and replay.admission_requested_by == "employee-boss"
    assert cross_tenant is None


async def test_claim_ordered_breaks_equal_counts_by_ready_at_then_need_id(
    integration_engine: AsyncEngine,
) -> None:
    """同簇规模先等最久 Need；同等待时间的 snapshot ID 不得抢在 Need ID 前。"""
    tenant = TenantId("tn_admission_ties")
    sessions = _sessions(integration_engine)
    bundles = (
        _bundle(
            tenant,
            "tie_late",
            3,
            ready_at=READY_AT + timedelta(hours=1),
            snapshot_id="sps_000_late",
        ),
        _bundle(
            tenant,
            "a_tie",
            3,
            snapshot_id="sps_zzz_need_a",
        ),
        _bundle(
            tenant,
            "b_tie",
            3,
            snapshot_id="sps_aaa_need_b",
        ),
    )
    for admission, snapshot in bundles:
        await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        claimed = await uow.admissions.claim_ordered(
            tenant,
            limit=3,
            claim_token="claim-ties",
            claim_expires_at=NOW + timedelta(minutes=10),
            now=NOW + timedelta(minutes=1),
        )

    assert [item.need_id for item in claimed] == [
        ValidatedNeedId("need_a_tie"),
        ValidatedNeedId("need_b_tie"),
        ValidatedNeedId("need_tie_late"),
    ]


@pytest.mark.parametrize("limit", [True, 0, -1, 51, 1.5])
async def test_claim_rejects_non_exact_or_out_of_range_limit_before_io(
    limit: object,
) -> None:
    tenant = TenantId("tn_admission_claim_limit")
    repository = _exploding_repository(tenant)

    with pytest.raises(ValidationError, match="limit"):
        await repository.claim_ordered(
            tenant,
            cast(int, limit),
            "claim-limit",
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize("limit", [True, 0, -1, 201, 1.5])
async def test_list_rejects_non_exact_or_out_of_range_limit_before_io(
    limit: object,
) -> None:
    tenant = TenantId("tn_admission_list_limit")
    repository = _exploding_repository(tenant)

    with pytest.raises(ValidationError, match="limit"):
        await repository.list_by_state(tenant, AdmissionState.WAITING, cast(int, limit))


@pytest.mark.parametrize("bad_token", ["", " token", "token\nvalue", "x" * 201])
@pytest.mark.parametrize("operation", ["claim", "complete", "release", "block"])
async def test_matching_tokens_are_validated_before_io(
    bad_token: str,
    operation: str,
) -> None:
    tenant = TenantId("tn_admission_token_validation")
    admission_id = SourcingAdmissionId("adm_token_validation")
    repository = _exploding_repository(tenant)

    with pytest.raises(ValidationError, match="claim_token"):
        if operation == "claim":
            await repository.claim_ordered(
                tenant,
                1,
                bad_token,
                NOW + timedelta(minutes=2),
                NOW + timedelta(minutes=1),
            )
        elif operation == "complete":
            await repository.complete(
                tenant,
                admission_id,
                bad_token,
                RunId("run_token_validation"),
                "scheduler",
                NOW + timedelta(minutes=1),
            )
        elif operation == "release":
            await repository.release(
                tenant, admission_id, bad_token, NOW + timedelta(minutes=1)
            )
        else:
            await repository.block(
                tenant,
                admission_id,
                AdmissionBlockedReason.CASE_STATE_MISMATCH,
                NOW + timedelta(minutes=1),
                claim_token=bad_token,
            )


@pytest.mark.parametrize("bad_system_actor", [" scheduler", "worker\x7f", "x" * 201])
async def test_complete_validates_system_actor_before_io(
    bad_system_actor: str,
) -> None:
    tenant = TenantId("tn_admission_system_actor")
    repository = _exploding_repository(tenant)

    with pytest.raises(ValidationError, match="system_actor_id"):
        await repository.complete(
            tenant,
            SourcingAdmissionId("adm_system_actor"),
            "claim-valid",
            RunId("run_system_actor"),
            bad_system_actor,
            NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    "bad_time",
    [
        NOW.replace(tzinfo=None),
        datetime(2026, 9, 2, 20, 1, tzinfo=timezone(timedelta(hours=8))),
    ],
)
@pytest.mark.parametrize(
    "operation",
    ["claim_now", "claim_expiry", "complete", "release", "expired", "block"],
)
async def test_state_write_times_must_be_utc_before_io(
    bad_time: datetime,
    operation: str,
) -> None:
    tenant = TenantId("tn_admission_time_validation")
    admission_id = SourcingAdmissionId("adm_time_validation")
    repository = _exploding_repository(tenant)

    with pytest.raises(ValidationError, match="UTC"):
        if operation == "claim_now":
            await repository.claim_ordered(
                tenant, 1, "claim-valid", NOW + timedelta(minutes=2), bad_time
            )
        elif operation == "claim_expiry":
            await repository.claim_ordered(
                tenant, 1, "claim-valid", bad_time, NOW + timedelta(minutes=1)
            )
        elif operation == "complete":
            await repository.complete(
                tenant,
                admission_id,
                "claim-valid",
                RunId("run_time_validation"),
                "scheduler",
                bad_time,
            )
        elif operation == "release":
            await repository.release(tenant, admission_id, "claim-valid", bad_time)
        elif operation == "expired":
            await repository.release_expired_claims(tenant, bad_time)
        else:
            await repository.block(
                tenant,
                admission_id,
                AdmissionBlockedReason.CASE_STATE_MISMATCH,
                bad_time,
            )


async def test_claim_requires_expiry_strictly_after_now_before_io() -> None:
    tenant = TenantId("tn_admission_expiry_validation")
    repository = _exploding_repository(tenant)

    with pytest.raises(ValidationError, match="claim_expires_at"):
        await repository.claim_ordered(
            tenant,
            1,
            "claim-valid",
            NOW + timedelta(minutes=1),
            NOW + timedelta(minutes=1),
        )


async def test_expired_claim_is_reclaimed_and_starting_block_requires_token(
    integration_engine: AsyncEngine,
) -> None:
    """过期租约可被新 worker 接管；错误 token 不能阻断新的 starting owner。"""
    tenant = TenantId("tn_admission_lease")
    admission, snapshot = _bundle(tenant, "lease", 1)
    sessions = _sessions(integration_engine)
    await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        first = await uow.admissions.claim_ordered(
            tenant,
            1,
            "old-token",
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=1),
        )
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        reclaimed = await uow.admissions.claim_ordered(
            tenant,
            1,
            "new-token",
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=3),
        )
        wrong = await uow.admissions.block(
            tenant,
            admission.admission_id,
            AdmissionBlockedReason.CASE_STATE_MISMATCH,
            NOW + timedelta(minutes=4),
            claim_token="old-token",
        )
        blocked = await uow.admissions.block(
            tenant,
            admission.admission_id,
            AdmissionBlockedReason.CASE_STATE_MISMATCH,
            NOW + timedelta(minutes=4),
            claim_token="new-token",
        )

    assert first[0].claim_token == "old-token"
    assert reclaimed[0].claim_token == "new-token"
    assert wrong is None
    assert blocked is not None
    assert blocked.state is AdmissionState.BLOCKED


async def test_complete_release_and_expired_release_persist_successfully(
    integration_engine: AsyncEngine,
) -> None:
    """三个租约终结路径都应在真实 PostgreSQL 上写入合法状态。"""
    tenant = TenantId("tn_admission_transitions")
    sessions = _sessions(integration_engine)
    bundles = {
        "release_ok": _bundle(
            tenant, "release_ok", 1, ready_at=NOW - timedelta(days=4)
        ),
        "complete_ok": _bundle(
            tenant, "complete_ok", 1, ready_at=NOW - timedelta(days=3)
        ),
        "expired_ok": _bundle(tenant, "expired_ok", 1),
    }
    for admission, snapshot in bundles.values():
        await _store_bundle(integration_engine, sessions, admission, snapshot)

    claimed: dict[str, SourcingAdmission] = {}
    for suffix, (admission, _) in bundles.items():
        expiry = (
            NOW + timedelta(minutes=2)
            if suffix == "expired_ok"
            else NOW + timedelta(minutes=10)
        )
        async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
            result = await uow.admissions.claim_ordered(
                tenant,
                1,
                f"claim-{suffix}",
                expiry,
                NOW + timedelta(minutes=1),
            )
        claimed[suffix] = next(
            item for item in result if item.admission_id == admission.admission_id
        )

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        released = await uow.admissions.release(
            tenant,
            claimed["release_ok"].admission_id,
            "claim-release_ok",
            NOW + timedelta(minutes=2),
        )
        completed = await uow.admissions.complete(
            tenant,
            claimed["complete_ok"].admission_id,
            "claim-complete_ok",
            RunId("run_complete_ok"),
            "scheduler",
            NOW + timedelta(minutes=2),
        )
        expired = await uow.admissions.release_expired_claims(
            tenant, NOW + timedelta(minutes=3)
        )

    assert released is not None and released.state is AdmissionState.WAITING
    assert completed is not None and completed.state is AdmissionState.ADMITTED
    assert completed.admitted_by == "scheduler"
    assert [item.admission_id for item in expired] == [
        claimed["expired_ok"].admission_id
    ]
    assert expired[0].state is AdmissionState.WAITING


async def test_backward_transition_times_do_not_mutate_rows(
    integration_engine: AsyncEngine,
) -> None:
    """合法 UTC 但早于 updated_at 的写入应条件失配，而不是倒拨状态时钟。"""
    tenant = TenantId("tn_admission_monotonic")
    sessions = _sessions(integration_engine)
    suffixes = ("claim_back", "release_back", "complete_back", "block_back")
    bundles = {
        "claim_back": _bundle(tenant, "claim_back", 1),
        "release_back": _bundle(
            tenant, "release_back", 1, ready_at=NOW - timedelta(days=5)
        ),
        "complete_back": _bundle(
            tenant, "complete_back", 1, ready_at=NOW - timedelta(days=4)
        ),
        "block_back": _bundle(
            tenant, "block_back", 1, ready_at=NOW - timedelta(days=3)
        ),
    }
    for admission, snapshot in bundles.values():
        await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        not_claimed = await uow.admissions.claim_ordered(
            tenant,
            1,
            "claim-backward",
            NOW + timedelta(minutes=1),
            NOW - timedelta(minutes=1),
        )
    assert not_claimed == []

    claimed: dict[str, SourcingAdmission] = {}
    for suffix in suffixes[1:]:
        async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
            result = await uow.admissions.claim_ordered(
                tenant,
                1,
                f"claim-{suffix}",
                NOW + timedelta(minutes=10),
                NOW + timedelta(minutes=1),
            )
        claimed[suffix] = next(
            item
            for item in result
            if item.admission_id == bundles[suffix][0].admission_id
        )

    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        released = await uow.admissions.release(
            tenant,
            claimed["release_back"].admission_id,
            "claim-release_back",
            NOW,
        )
        completed = await uow.admissions.complete(
            tenant,
            claimed["complete_back"].admission_id,
            "claim-complete_back",
            RunId("run_complete_back"),
            "scheduler",
            NOW,
        )
        blocked = await uow.admissions.block(
            tenant,
            claimed["block_back"].admission_id,
            AdmissionBlockedReason.CASE_STATE_MISMATCH,
            NOW,
            claim_token="claim-block_back",
        )

    assert released is None
    assert completed is None
    assert blocked is None
    async with SqlAlchemySourcingUnitOfWork(sessions, tenant) as uow:
        unchanged = {
            suffix: await uow.admissions.get(tenant, admission.admission_id)
            for suffix, (admission, _) in bundles.items()
        }
    assert unchanged["claim_back"] == bundles["claim_back"][0]
    for suffix in suffixes[1:]:
        assert unchanged[suffix] == claimed[suffix]


async def test_repository_never_reads_or_updates_cross_tenant_admission(
    integration_engine: AsyncEngine,
) -> None:
    """若查询遗漏 tenant predicate，异租户可观察或完成 owner 的 admission。"""
    owner = TenantId("tn_admission_owner")
    stranger = TenantId("tn_admission_stranger")
    admission, snapshot = _bundle(owner, "tenant_scope", 1)
    sessions = _sessions(integration_engine)
    await _store_bundle(integration_engine, sessions, admission, snapshot)

    async with SqlAlchemySourcingUnitOfWork(sessions, stranger) as uow:
        assert await uow.admissions.get(stranger, admission.admission_id) is None
        assert (
            await uow.admissions.complete(
                stranger,
                admission.admission_id,
                "not-owner",
                RunId("run_not_owner"),
                "scheduler",
                NOW + timedelta(minutes=1),
            )
            is None
        )
        assert (
            await uow.admissions.block(
                stranger,
                admission.admission_id,
                AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
                NOW + timedelta(minutes=1),
            )
            is None
        )
        with pytest.raises(ValueError, match="租户"):
            await uow.admissions.get(owner, admission.admission_id)

    async with SqlAlchemySourcingUnitOfWork(sessions, owner) as uow:
        unchanged = await uow.admissions.get(owner, admission.admission_id)
    assert unchanged == admission
