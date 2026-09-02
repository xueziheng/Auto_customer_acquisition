"""PostgreSQL 寻源准入仓储：原子首快照、排序 claim 与租户隔离。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

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
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import SourcingAdmissionRow, SourcingPrioritySnapshotRow
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
        ready_at=READY_AT,
        facts_observed_at=created_at,
        ranking_version=ADMISSION_RANKING_VERSION,
    )
    admission = SourcingAdmission(
        tenant_id=tenant,
        admission_id=admission_id,
        case_id=case_id,
        need_id=need_id,
        state=state,
        ready_at=READY_AT,
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
        ready_at=READY_AT,
        ranking_version=ADMISSION_RANKING_VERSION,
        facts_observed_at=created_at,
        facts_hash=facts_hash,
        created_at=created_at,
    )
    return admission, snapshot


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
