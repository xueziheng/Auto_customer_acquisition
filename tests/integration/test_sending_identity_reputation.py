"""发件身份信誉熔断的真实 PostgreSQL 事务、隔离与并发证明。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.sending_identity.schemas import (
    AuthenticationResult,
    DeliveryEventRecord,
    IdentityRegisterRequest,
)
from shared.schemas.identifiers import (
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.integration.test_sending_identity_reservations import (
    _NOW,
    _Audit,
    _boss,
    _ready_identity,
    _service,
    _system,
)

_models = importlib.import_module("domains.sending_identity.models")
DeliveryEventType = _models.DeliveryEventType
DomainRole = _models.DomainRole
IdentityState = _models.IdentityState
ReputationMetric = _models.ReputationMetric
ReputationSeverity = _models.ReputationSeverity


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _event(
    tenant: TenantId,
    identity_id: SendingIdentityId,
    *,
    key: str,
    event_type: DeliveryEventType,
    occurred_at=_NOW,
) -> DeliveryEventRecord:
    return DeliveryEventRecord(
        tenant_id=tenant,
        identity_id=identity_id,
        event_type=event_type,
        occurred_at=occurred_at,
        dedup_key=IdempotencyKey(key),
        source_ref=f"provider-{key}",
    )


async def _seed_window_facts(
    sf: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    reservations: dict[SendingIdentityId, int],
    reputation_events: list[tuple[SendingIdentityId, DeliveryEventType, str]],
) -> None:
    """只造不可变窗口事实；状态变更必须继续经 public service。"""
    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        for identity_id, count in reservations.items():
            for sequence in range(1, count + 1):
                session.add(
                    tables.SendReservationRow(
                        tenant_id=tenant,
                        reservation_id=new_id("res"),
                        identity_id=identity_id,
                        reservation_key=f"seed:{identity_id[-4:]}:{sequence}",
                        on_day=_NOW.date(),
                        sequence=sequence,
                        created_at=_NOW,
                    )
                )
        for identity_id, event_type, key in reputation_events:
            session.add(
                tables.ReputationEventRow(
                    tenant_id=tenant,
                    reputation_event_id=new_id("rep"),
                    identity_id=identity_id,
                    event_type=event_type.value,
                    occurred_at=_NOW,
                    dedup_key=key,
                    source_ref=f"seed-{key}",
                    created_at=_NOW,
                )
            )
        await session.commit()


async def _stored_summary(
    sf: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    identity_ids: tuple[SendingIdentityId, ...],
) -> tuple[list[str], int, int, list[object]]:
    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        states = list(
            (
                await session.execute(
                    select(tables.SendingIdentityRow.state)
                    .where(
                        tables.SendingIdentityRow.tenant_id == tenant,
                        tables.SendingIdentityRow.identity_id.in_(identity_ids),
                    )
                    .order_by(tables.SendingIdentityRow.identity_id)
                )
            ).scalars()
        )
        event_count = int(
            (
                await session.execute(
                    select(func.count(tables.ReputationEventRow.reputation_event_id)).where(
                        tables.ReputationEventRow.tenant_id == tenant
                    )
                )
            ).scalar_one()
        )
        action_count = int(
            (
                await session.execute(
                    select(func.count(tables.IdentityActionRow.action_id)).where(
                        tables.IdentityActionRow.tenant_id == tenant
                    )
                )
            ).scalar_one()
        )
        outbox = list(
            (
                await session.execute(
                    select(tables.OutboxEventRow)
                    .where(tables.OutboxEventRow.tenant_id == tenant)
                    .order_by(tables.OutboxEventRow.event_type, tables.OutboxEventRow.event_id)
                )
            ).scalars()
        )
    return states, event_count, action_count, outbox


def _payload_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for child in value.values()
            for key in _payload_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _payload_keys(child)}
    return set()


async def test_event_state_action_and_safe_outbox_commit_atomically(
    engine_fx: AsyncEngine,
) -> None:
    """第 5 个硬退信在同一事务写事实、停用、action 和两条安全 outbox。"""
    tenant = TenantId("tRepAtomic")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local="atomic",
        domain="cold.rep-atomic.example",
    )
    await _seed_window_facts(
        sf,
        tenant,
        {identity_id: 100},
        [
            (identity_id, DeliveryEventType.HARD_BOUNCED, f"seed-hard-{index}")
            for index in range(4)
        ],
    )
    audit.records.clear()

    assert await service.record_delivery_event(
        tenant,
        identity_id,
        _event(
            tenant,
            identity_id,
            key="atomic-hard-five",
            event_type=DeliveryEventType.HARD_BOUNCED,
        ),
        actor=_system(identity_id),
    )

    states, event_count, action_count, outbox = await _stored_summary(
        sf, tenant, (identity_id,)
    )
    assert states == [IdentityState.SUSPENDED.value]
    assert event_count == 5
    assert action_count == 4  # register/auth begin/warmup + reputation restriction
    assert [row.event_type for row in outbox] == [
        "ReputationThresholdBreached",
        "SendingIdentitySuspended",
    ]
    for row in outbox:
        assert _payload_keys(row.event_payload).isdisjoint(
            {"address", "domain", "source_ref", "ref", "note"}
        )
    assert len(audit.records) == 1


@pytest.mark.parametrize(
    (
        "offset",
        "event_type",
        "reservations",
        "existing_events",
        "expected_state",
        "expected_metric",
        "expected_value",
        "expected_threshold",
        "expected_state_event",
    ),
    [
        (
            timedelta(microseconds=1),
            DeliveryEventType.SPAM_TRAP,
            0,
            (),
            IdentityState.SUSPENDED,
            ReputationMetric.SPAM_TRAP,
            "1",
            "0",
            "SendingIdentitySuspended",
        ),
        (
            timedelta(minutes=5),
            DeliveryEventType.BLOCKLISTED,
            0,
            (),
            IdentityState.SUSPENDED,
            ReputationMetric.BLOCKLISTED,
            "1",
            "0",
            "SendingIdentitySuspended",
        ),
        (
            timedelta(minutes=5),
            DeliveryEventType.HARD_BOUNCED,
            100,
            (DeliveryEventType.HARD_BOUNCED, DeliveryEventType.HARD_BOUNCED),
            IdentityState.THROTTLED,
            ReputationMetric.HARD_BOUNCE_RATE,
            "0.03",
            "0.03",
            "SendingIdentityThrottled",
        ),
        (
            timedelta(minutes=5),
            DeliveryEventType.COMPLAINT,
            1000,
            (),
            IdentityState.THROTTLED,
            ReputationMetric.COMPLAINT_RATE,
            "0.001",
            "0.001",
            "SendingIdentityThrottled",
        ),
    ],
)
async def test_accepted_future_event_is_visible_to_same_transaction_evaluation(
    engine_fx: AsyncEngine,
    offset: timedelta,
    event_type: DeliveryEventType,
    reservations: int,
    existing_events: tuple[DeliveryEventType, ...],
    expected_state: IdentityState,
    expected_metric: ReputationMetric,
    expected_value: str,
    expected_threshold: str,
    expected_state_event: str,
) -> None:
    """容差内 future fact 必须在本次事务触发 immediate/ratio fuse。"""
    suffix = (
        f"{event_type.value}-{offset.total_seconds():g}"
        .replace(".", "-")
        .replace("_", "-")
    )
    tenant = TenantId(
        f"tRepF{event_type.value.replace('_', '').title()}{reservations}"
    )
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local=f"future{reservations}",
        domain=f"cold.rep-future-{suffix}.example",
    )
    await _seed_window_facts(
        sf,
        tenant,
        {identity_id: reservations} if reservations else {},
        [
            (identity_id, seeded_type, f"seed-future-{index}")
            for index, seeded_type in enumerate(existing_events)
        ],
    )
    audit.records.clear()
    assert await service.record_delivery_event(
        tenant,
        identity_id,
        _event(
            tenant,
            identity_id,
            key=f"future-{suffix}",
            event_type=event_type,
            occurred_at=_NOW + offset,
        ),
        actor=_system(identity_id),
    )

    tables = importlib.import_module("infra.db.tables")
    states, event_count, action_count, outbox = await _stored_summary(
        sf, tenant, (identity_id,)
    )
    assert states == [expected_state.value]
    assert event_count == len(existing_events) + 1
    assert action_count == 4
    assert len(audit.records) == 1
    assert {row.event_type for row in outbox} == {
        expected_state_event,
        "ReputationThresholdBreached",
    }
    breach = next(
        row for row in outbox if row.event_type == "ReputationThresholdBreached"
    )
    assert (
        breach.event_payload["sending_identity_id"],
        breach.event_payload["metric"],
        Decimal(str(breach.event_payload["value"])),
        Decimal(str(breach.event_payload["threshold"])),
        breach.event_payload["severity"],
    ) == (
        identity_id,
        expected_metric.value,
        Decimal(expected_value),
        Decimal(expected_threshold),
        expected_state.value,
    )
    async with sf() as session:
        actions = list(
            (
                await session.execute(
                    select(tables.IdentityActionRow).where(
                        tables.IdentityActionRow.tenant_id == tenant,
                        tables.IdentityActionRow.action == "delivery_event:record",
                    )
                )
            ).scalars()
        )
    assert [
        (row.identity_id, row.before_state, row.after_state) for row in actions
    ] == [(identity_id, IdentityState.WARMING.value, expected_state.value)]


async def test_future_ratio_event_preserves_now_anchored_lower_window_boundary(
    engine_fx: AsyncEngine,
) -> None:
    """future 硬退信须纳入 now 窗口，不得提前淘汰下界内事实。"""
    tenant = TenantId("tRepFutureLowerBoundary")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local="future-lower-boundary",
        domain="cold.rep-future-lower-boundary.example",
    )
    await _seed_window_facts(sf, tenant, {identity_id: 100}, [])
    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        result = await session.execute(
            update(tables.SendingIdentityRow)
            .where(
                tables.SendingIdentityRow.tenant_id == tenant,
                tables.SendingIdentityRow.identity_id == identity_id,
            )
            .values(created_at=_NOW - timedelta(days=8))
        )
        assert result.rowcount == 1
        for index, occurred_at in enumerate(
            (_NOW - timedelta(days=7) + timedelta(microseconds=1), _NOW)
        ):
            session.add(
                tables.ReputationEventRow(
                    tenant_id=tenant,
                    reputation_event_id=new_id("rep"),
                    identity_id=identity_id,
                    event_type=DeliveryEventType.HARD_BOUNCED.value,
                    occurred_at=occurred_at,
                    dedup_key=f"lower-bound-hard-{index}",
                    source_ref=f"seed-lower-bound-hard-{index}",
                    created_at=occurred_at,
                )
            )
        await session.commit()
    audit.records.clear()

    assert await service.record_delivery_event(
        tenant,
        identity_id,
        _event(
            tenant,
            identity_id,
            key="future-lower-bound-hard-three",
            event_type=DeliveryEventType.HARD_BOUNCED,
            occurred_at=_NOW + timedelta(minutes=5),
        ),
        actor=_system(identity_id),
    )

    states, event_count, action_count, outbox = await _stored_summary(
        sf, tenant, (identity_id,)
    )
    assert states == [IdentityState.THROTTLED.value]
    assert (event_count, action_count, len(outbox), len(audit.records)) == (3, 4, 2, 1)
    assert {row.event_type for row in outbox} == {
        "SendingIdentityThrottled",
        "ReputationThresholdBreached",
    }
    breach = next(
        row for row in outbox if row.event_type == "ReputationThresholdBreached"
    )
    assert (
        breach.event_payload["sending_identity_id"],
        breach.event_payload["metric"],
        Decimal(str(breach.event_payload["value"])),
        Decimal(str(breach.event_payload["threshold"])),
        breach.event_payload["severity"],
    ) == (
        identity_id,
        ReputationMetric.HARD_BOUNCE_RATE.value,
        Decimal(".03"),
        Decimal(".03"),
        ReputationSeverity.THROTTLED.value,
    )
    async with sf() as session:
        action = (
            await session.execute(
                select(tables.IdentityActionRow).where(
                    tables.IdentityActionRow.tenant_id == tenant,
                    tables.IdentityActionRow.action == "delivery_event:record",
                )
            )
        ).scalar_one()
    assert (
        action.identity_id,
        action.before_state,
        action.after_state,
    ) == (
        identity_id,
        IdentityState.WARMING.value,
        IdentityState.THROTTLED.value,
    )


async def test_manual_evaluation_remains_anchored_at_service_now(
    engine_fx: AsyncEngine,
) -> None:
    """manual evaluate 不得将尚未到时的事件纳入 now 窗口。"""
    tenant = TenantId("tRepManualNowBoundary")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    service = _service(sf, tenant, _Audit())
    identity_id = await _ready_identity(
        service,
        tenant,
        local="manual-now-boundary",
        domain="cold.rep-manual-now-boundary.example",
    )
    await _seed_window_facts(
        sf,
        tenant,
        {identity_id: 100},
        [
            (identity_id, DeliveryEventType.HARD_BOUNCED, "manual-now-hard-0"),
            (identity_id, DeliveryEventType.HARD_BOUNCED, "manual-now-hard-1"),
        ],
    )
    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        session.add(
            tables.ReputationEventRow(
                tenant_id=tenant,
                reputation_event_id=new_id("rep"),
                identity_id=identity_id,
                event_type=DeliveryEventType.HARD_BOUNCED.value,
                occurred_at=_NOW + timedelta(minutes=5),
                dedup_key="manual-future-hard",
                source_ref="seed-manual-future-hard",
                created_at=_NOW + timedelta(minutes=5),
            )
        )
        await session.commit()

    view = await service.evaluate_reputation(
        tenant, identity_id, actor=_system(identity_id)
    )

    states, event_count, action_count, outbox = await _stored_summary(
        sf, tenant, (identity_id,)
    )
    assert states == [IdentityState.WARMING.value]
    assert view.hard_bounce_rate == Decimal(".02")
    assert (event_count, action_count, outbox) == (3, 3, [])


class _FailingCommitSession(AsyncSession):
    async def commit(self) -> None:
        await self.flush()
        raise RuntimeError("private reputation commit failure")


async def test_commit_failure_rolls_back_event_state_action_outbox_and_allow(
    engine_fx: AsyncEngine,
) -> None:
    """flush 后 commit failure 也必须回滚四类业务写且零 allow。"""
    tenant = TenantId("tRepCommitFail")
    normal_sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    seed_service = _service(normal_sf, tenant, _Audit())
    identity_id = await _ready_identity(
        seed_service,
        tenant,
        local="commit",
        domain="cold.rep-commit.example",
    )
    baseline = await _stored_summary(normal_sf, tenant, (identity_id,))
    failing_sf = async_sessionmaker(
        bind=engine_fx,
        expire_on_commit=False,
        class_=_FailingCommitSession,
    )
    audit = _Audit()
    failing_service = _service(failing_sf, tenant, audit)
    with pytest.raises(RuntimeError, match="private reputation commit failure"):
        await failing_service.record_delivery_event(
            tenant,
            identity_id,
            _event(
                tenant,
                identity_id,
                key="commit-spam-trap",
                event_type=DeliveryEventType.SPAM_TRAP,
                occurred_at=_NOW + timedelta(minutes=5),
            ),
            actor=_system(identity_id),
        )
    assert await _stored_summary(normal_sf, tenant, (identity_id,)) == baseline
    assert audit.records == []


async def test_concurrent_duplicate_event_inserts_once_and_audits_every_retry(
    engine_fx: AsyncEngine,
) -> None:
    """相同 dedup 的并发 webhook 只落一个事实，其余是成功幂等 retry。"""
    tenant = TenantId("tRepDedupRace")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local="dedup",
        domain="cold.rep-dedup.example",
    )
    audit.records.clear()
    event = _event(
        tenant,
        identity_id,
        key="race-delivered",
        event_type=DeliveryEventType.DELIVERED,
    )
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                service.record_delivery_event(
                    tenant, identity_id, event, actor=_system(identity_id)
                )
                for _ in range(20)
            )
        ),
        timeout=20,
    )
    assert results.count(True) == 1
    assert results.count(False) == 19
    states, event_count, action_count, outbox = await _stored_summary(
        sf, tenant, (identity_id,)
    )
    assert states == [IdentityState.WARMING.value]
    assert (event_count, action_count, outbox) == (1, 3, [])
    assert len(audit.records) == 20


async def test_concurrent_domain_suspension_has_no_deadlock_or_duplicate_outbox(
    engine_fx: AsyncEngine,
) -> None:
    """两个 identity 同时跨过 domain 阈值，全部停用且状态事件恰各一条。"""
    tenant = TenantId("tRepDomainRace")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    domain = "cold.rep-domain-race.example"
    first = await _ready_identity(service, tenant, local="first", domain=domain)
    second = await _ready_identity(service, tenant, local="second", domain=domain)
    await _seed_window_facts(
        sf,
        tenant,
        {first: 25, second: 25},
        [
            (first, DeliveryEventType.HARD_BOUNCED, "seed-domain-hard-1"),
            (second, DeliveryEventType.HARD_BOUNCED, "seed-domain-hard-2"),
        ],
    )
    audit.records.clear()
    results = await asyncio.wait_for(
        asyncio.gather(
            service.record_delivery_event(
                tenant,
                first,
                _event(
                    tenant,
                    first,
                    key="race-domain-hard-first",
                    event_type=DeliveryEventType.HARD_BOUNCED,
                ),
                actor=_system(first),
            ),
            service.record_delivery_event(
                tenant,
                second,
                _event(
                    tenant,
                    second,
                    key="race-domain-hard-second",
                    event_type=DeliveryEventType.HARD_BOUNCED,
                ),
                actor=_system(second),
            ),
        ),
        timeout=20,
    )
    assert results == [True, True]
    states, event_count, action_count, outbox = await _stored_summary(
        sf, tenant, (first, second)
    )
    assert states == [IdentityState.SUSPENDED.value, IdentityState.SUSPENDED.value]
    assert event_count == 4
    assert action_count == 8  # two lifecycle triplets + two restriction actions
    assert [row.event_type for row in outbox].count("SendingIdentitySuspended") == 2
    assert [row.event_type for row in outbox].count("ReputationThresholdBreached") == 1
    assert len(outbox) == 3
    assert len(audit.records) == 2


async def test_domain_fanout_persists_exact_action_and_outbox_ownership(
    engine_fx: AsyncEngine,
) -> None:
    """确定 source 的域熔断逐 identity 写 action/state event，breach 只归 source。"""
    tenant = TenantId("tRepDomainOwnership")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    domain = "cold.rep-domain-ownership.example"
    source = await _ready_identity(service, tenant, local="source", domain=domain)
    sibling = await _ready_identity(service, tenant, local="sibling", domain=domain)
    await _seed_window_facts(
        sf,
        tenant,
        {source: 25, sibling: 25},
        [
            (source, DeliveryEventType.HARD_BOUNCED, f"ownership-hard-{index}")
            for index in range(3)
        ],
    )
    audit.records.clear()
    await service.evaluate_reputation(tenant, source, actor=_system(source))

    tables = importlib.import_module("infra.db.tables")
    states, event_count, action_count, outbox = await _stored_summary(
        sf, tenant, (source, sibling)
    )
    assert states == [IdentityState.SUSPENDED.value, IdentityState.SUSPENDED.value]
    assert (event_count, action_count, len(outbox)) == (3, 8, 3)
    async with sf() as session:
        actions = list(
            (
                await session.execute(
                    select(tables.IdentityActionRow)
                    .where(
                        tables.IdentityActionRow.tenant_id == tenant,
                        tables.IdentityActionRow.action == "reputation:evaluate",
                    )
                    .order_by(tables.IdentityActionRow.identity_id)
                )
            ).scalars()
        )
    assert {
        (row.identity_id, row.before_state, row.after_state, row.action)
        for row in actions
    } == {
        (
            source,
            IdentityState.WARMING.value,
            IdentityState.SUSPENDED.value,
            "reputation:evaluate",
        ),
        (
            sibling,
            IdentityState.WARMING.value,
            IdentityState.SUSPENDED.value,
            "reputation:evaluate",
        ),
    }
    state_rows = [
        row for row in outbox if row.event_type == "SendingIdentitySuspended"
    ]
    assert {
        (
            row.event_payload["sending_identity_id"],
            row.event_payload["reason"],
        )
        for row in state_rows
    } == {
        (source, ReputationMetric.HARD_BOUNCE_RATE.value),
        (sibling, ReputationMetric.HARD_BOUNCE_RATE.value),
    }
    breaches = [
        row for row in outbox if row.event_type == "ReputationThresholdBreached"
    ]
    assert len(breaches) == 1
    assert (
        breaches[0].event_payload["sending_identity_id"],
        breaches[0].event_payload["metric"],
        Decimal(str(breaches[0].event_payload["value"])),
        Decimal(str(breaches[0].event_payload["threshold"])),
        breaches[0].event_payload["severity"],
    ) == (
        source,
        ReputationMetric.HARD_BOUNCE_RATE.value,
        Decimal("0.06"),
        Decimal("0.05"),
        ReputationSeverity.SUSPENDED.value,
    )

    business_counts = (event_count, action_count, len(outbox))
    await service.evaluate_reputation(tenant, source, actor=_system(source))
    _, repeated_events, repeated_actions, repeated_outbox = await _stored_summary(
        sf, tenant, (source, sibling)
    )
    assert (repeated_events, repeated_actions, len(repeated_outbox)) == business_counts


async def _ready_identity_with_role(
    service,
    tenant: TenantId,
    *,
    local: str,
    domain: str,
    role: DomainRole,
) -> SendingIdentityId:
    identity_id = await service.register(
        tenant,
        IdentityRegisterRequest(
            address=f"{local}@{domain}",
            domain=domain,
            role=role,
        ),
        actor=_boss(),
    )
    await service.begin_authentication(tenant, identity_id, actor=_boss())
    await service.record_authentication_result(
        tenant,
        identity_id,
        AuthenticationResult(
            checked_at=_NOW,
            spf_passed=True,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref=f"auth-{local}",
        ),
        actor=_system(identity_id),
    )
    await service.start_warmup(tenant, identity_id, 5, actor=_boss())
    return identity_id


async def test_same_domain_other_tenant_and_role_never_enters_aggregate(
    engine_fx: AsyncEngine,
) -> None:
    """同名 domain 的另一 tenant 即使 PRIMARY_BUSINESS 有危害也不能熔断目标。"""
    target_tenant = TenantId("tRepIsolateTarget")
    other_tenant = TenantId("tRepIsolateOther")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    target_service = _service(sf, target_tenant, _Audit())
    other_service = _service(sf, other_tenant, _Audit())
    domain = "shared.rep-isolation.example"
    target = await _ready_identity_with_role(
        target_service,
        target_tenant,
        local="target",
        domain=domain,
        role=DomainRole.COLD_OUTREACH,
    )
    other = await _ready_identity_with_role(
        other_service,
        other_tenant,
        local="other",
        domain=domain,
        role=DomainRole.PRIMARY_BUSINESS,
    )
    await _seed_window_facts(
        sf,
        other_tenant,
        {other: 50},
        [(other, DeliveryEventType.BLOCKLISTED, "other-tenant-blocklist")],
    )
    await target_service.evaluate_reputation(
        target_tenant, target, actor=_system(target)
    )
    states, event_count, action_count, outbox = await _stored_summary(
        sf, target_tenant, (target,)
    )
    assert states == [IdentityState.WARMING.value]
    assert (event_count, action_count, outbox) == (0, 3, [])


async def test_domain_view_and_fuse_share_conservative_thresholds_with_tenant_filter(
    engine_fx: AsyncEngine,
) -> None:
    """真实 PG 证明 view/fuse 共用保守阈值，且不混入同名跨租户事实。"""
    tenant = TenantId("tRepViewFuseTarget")
    other_tenant = TenantId("tRepViewFuseOther")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    service = _service(sf, tenant, _Audit())
    other_service = _service(sf, other_tenant, _Audit())
    domain = "cold.rep-view-fuse.example"
    first = await _ready_identity(service, tenant, local="first", domain=domain)
    second = await _ready_identity(service, tenant, local="second", domain=domain)
    other = await _ready_identity(
        other_service,
        other_tenant,
        local="other",
        domain=domain,
    )
    lower_id, higher_id = sorted((first, second), key=str)
    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        for identity_id, minimum_sample in ((lower_id, 100), (higher_id, 10)):
            result = await session.execute(
                update(tables.SendingIdentityRow)
                .where(
                    tables.SendingIdentityRow.tenant_id == tenant,
                    tables.SendingIdentityRow.identity_id == identity_id,
                )
                .values(minimum_sample=minimum_sample)
            )
            assert result.rowcount == 1
        await session.commit()
    await _seed_window_facts(
        sf,
        tenant,
        {first: 25, second: 25},
        [
            (first, DeliveryEventType.HARD_BOUNCED, f"view-fuse-hard-{index}")
            for index in range(3)
        ],
    )
    await _seed_window_facts(sf, other_tenant, {other: 100}, [])

    before = await service.get_domain_reputation(tenant, domain, actor=_boss())
    assert before.identity_count == 2
    assert before.reputation.sent_attempts == 50
    assert before.reputation.hard_bounce_rate == Decimal(".06")
    assert before.reputation.sample_sufficient
    assert before.worst_identity_id is None

    await service.evaluate_reputation(tenant, first, actor=_system(first))
    after = await service.get_domain_reputation(tenant, domain, actor=_boss())

    assert after.reputation.sample_sufficient
    assert after.worst_identity_id == lower_id
    target_states, _, _, _ = await _stored_summary(sf, tenant, (first, second))
    other_states, _, _, _ = await _stored_summary(sf, other_tenant, (other,))
    assert target_states == [
        IdentityState.SUSPENDED.value,
        IdentityState.SUSPENDED.value,
    ]
    assert other_states == [IdentityState.WARMING.value]
