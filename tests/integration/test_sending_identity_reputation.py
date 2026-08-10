"""发件身份信誉熔断的真实 PostgreSQL 事务、隔离与并发证明。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
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
) -> DeliveryEventRecord:
    return DeliveryEventRecord(
        tenant_id=tenant,
        identity_id=identity_id,
        event_type=event_type,
        occurred_at=_NOW,
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
