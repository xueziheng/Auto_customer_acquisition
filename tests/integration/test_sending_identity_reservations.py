"""发件身份 reservation 的真实 PostgreSQL 并发与事务证明。"""
from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.sending_identity.errors import WarmupLimitExceededError
from domains.sending_identity.permissions import (
    Actor,
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityScope,
)
from domains.sending_identity.schemas import (
    AuthenticationResult,
    IdentityRegisterRequest,
    SendReservation,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, TenantId

_models = importlib.import_module("domains.sending_identity.models")
DomainRole = _models.DomainRole
IdentityState = _models.IdentityState
SendingDomain = importlib.import_module(
    "domains.sending_identity.repository"
).SendingDomain

_NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def log(self, **record: object) -> None:
        self.records.append(record)


def _boss() -> Actor:
    return Actor(
        actor_id="boss_reservation_pg",
        role="boss",
        scope=SendingIdentityScope(level=ScopeLevel.TENANT),
    )


def _system(identity_id: SendingIdentityId) -> Actor:
    return Actor(
        actor_id="system_reservation_pg",
        role="system",
        scope=SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
    )


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    audit: _Audit,
    *,
    uow_wrapper=None,
):
    Service = importlib.import_module(
        "domains.sending_identity.service_impl"
    ).SendingIdentityServiceImpl
    Uow = importlib.import_module(
        "infra.db.sending_identity_uow"
    ).SqlAlchemySendingIdentityUnitOfWork

    def factory(requested_tenant: TenantId):
        uow = Uow(session_factory, requested_tenant, now=lambda: _NOW)
        return uow if uow_wrapper is None else uow_wrapper(uow)

    return Service(
        factory,
        Phase1SendingIdentityAuthorizer(tenant),
        audit,
        now=lambda: _NOW,
    )


async def _ready_identity(
    service,
    tenant: TenantId,
    *,
    local: str,
    domain: str,
) -> SendingIdentityId:
    identity_id = await service.register(
        tenant,
        IdentityRegisterRequest(
            address=f"{local}@{domain}",
            domain=domain,
            role=DomainRole.COLD_OUTREACH,
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
            check_ref=f"auth_{local}",
        ),
        actor=_system(identity_id),
    )
    await service.start_warmup(tenant, identity_id, 5, actor=_boss())
    return identity_id


async def _attempt(
    service,
    tenant: TenantId,
    identity_id: SendingIdentityId,
    key: str,
):
    return await service.reserve_send_slot(
        tenant,
        identity_id,
        IdempotencyKey(key),
        True,
        actor=_system(identity_id),
    )


async def _stored_counts(
    session_factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    identity_id: SendingIdentityId,
) -> tuple[int, int, list[int]]:
    tables = importlib.import_module("infra.db.tables")
    async with session_factory() as session:
        counter = (
            await session.execute(
                select(tables.SendCounterRow.sent_attempts).where(
                    tables.SendCounterRow.tenant_id == tenant,
                    tables.SendCounterRow.identity_id == identity_id,
                    tables.SendCounterRow.on_day == _NOW.date(),
                )
            )
        ).scalar_one_or_none()
        sequences = (
            await session.execute(
                select(tables.SendReservationRow.sequence)
                .where(
                    tables.SendReservationRow.tenant_id == tenant,
                    tables.SendReservationRow.identity_id == identity_id,
                )
                .order_by(tables.SendReservationRow.sequence)
            )
        ).scalars().all()
    return int(counter or 0), len(sequences), list(sequences)


async def _force_database_allowed_active_state(
    session_factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    identity_id: SendingIdentityId,
    *,
    elapsed_days: int,
) -> None:
    """用真实 PostgreSQL 证明 0008 允许 ACTIVE + 未完成 warmup 的防御性场景。"""
    tables = importlib.import_module("infra.db.tables")
    async with session_factory() as session:
        result = await session.execute(
            update(tables.SendingIdentityRow)
            .where(
                tables.SendingIdentityRow.tenant_id == tenant,
                tables.SendingIdentityRow.identity_id == identity_id,
            )
            .values(
                state=IdentityState.ACTIVE.value,
                warmup_started_on=_NOW.date() - timedelta(days=elapsed_days),
                activated_at=_NOW,
            )
        )
        assert result.rowcount == 1
        await session.commit()


@pytest.mark.parametrize("elapsed_days", [0, 27])
async def test_database_allowed_early_active_has_fixed_diagnostic_denial(
    engine_fx: AsyncEngine,
    elapsed_days: int,
) -> None:
    """ACTIVE day 1/day 28 不得借持久化不一致绕过完整预热。"""
    tenant = TenantId(f"tReservationEarlyActiveCheck{elapsed_days}")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local=f"activecheck{elapsed_days}",
        domain=f"cold.active-check-{elapsed_days}.example",
    )
    await _force_database_allowed_active_state(
        sf, tenant, identity_id, elapsed_days=elapsed_days
    )
    audit.records.clear()

    permission = await service.check_send_permission(
        tenant, identity_id, True, actor=_boss()
    )

    assert (
        permission.allowed,
        permission.reason,
        permission.warmup_day,
        permission.is_warming,
    ) == (False, "发件身份预热尚未完成", elapsed_days + 1, False)
    assert await _stored_counts(sf, tenant, identity_id) == (0, 0, [])
    assert len(audit.records) == 1  # 诊断成功返回 deny permission，仍是授权读取


@pytest.mark.parametrize("elapsed_days", [0, 27])
async def test_database_allowed_early_active_cannot_reserve_or_audit_allow(
    engine_fx: AsyncEngine,
    elapsed_days: int,
) -> None:
    """authoritative reserve 对 ACTIVE day 1/day 28 固定 typed 拒绝且零写。"""
    tenant = TenantId(f"tReservationEarlyActiveReserve{elapsed_days}")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local=f"activereserve{elapsed_days}",
        domain=f"cold.active-reserve-{elapsed_days}.example",
    )
    await _force_database_allowed_active_state(
        sf, tenant, identity_id, elapsed_days=elapsed_days
    )
    audit.records.clear()

    with pytest.raises(WarmupLimitExceededError):
        await service.reserve_send_slot(
            tenant,
            identity_id,
            IdempotencyKey(f"early-active-{elapsed_days}"),
            True,
            actor=_system(identity_id),
        )

    assert await _stored_counts(sf, tenant, identity_id) == (0, 0, [])
    assert audit.records == []


async def test_database_active_on_day_twenty_nine_remains_sendable(
    engine_fx: AsyncEngine,
) -> None:
    """ACTIVE day 29 是完整预热后的合法边界。"""
    tenant = TenantId("tReservationActiveDay29")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local="activeday29",
        domain="cold.active-day-29.example",
    )
    await _force_database_allowed_active_state(
        sf, tenant, identity_id, elapsed_days=28
    )
    audit.records.clear()

    permission = await service.check_send_permission(
        tenant, identity_id, True, actor=_boss()
    )
    reservation = await service.reserve_send_slot(
        tenant,
        identity_id,
        IdempotencyKey("active-day-29"),
        True,
        actor=_system(identity_id),
    )

    assert (permission.allowed, permission.reason, permission.warmup_day) == (
        True,
        None,
        29,
    )
    assert (reservation.sequence, reservation.daily_limit) == (1, 5)
    assert await _stored_counts(sf, tenant, identity_id) == (1, 1, [1])
    assert len(audit.records) == 2


async def test_twenty_different_keys_stop_exactly_at_remaining_capacity(
    engine_fx: AsyncEngine,
) -> None:
    """count=3、limit=5 时 20 个独立事务只能再成功 2 个。"""
    tenant = TenantId("tReservationDifferentKeys")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local="different",
        domain="cold.reservation-different.example",
    )
    for index in range(3):
        await service.reserve_send_slot(
            tenant,
            identity_id,
            IdempotencyKey(f"seed-{index}"),
            True,
            actor=_system(identity_id),
        )
    audit.records.clear()

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                _attempt(service, tenant, identity_id, f"race-{index}")
                for index in range(20)
            ),
            return_exceptions=True,
        ),
        timeout=20,
    )

    successes = [item for item in results if isinstance(item, SendReservation)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(successes) == 2
    assert {item.sequence for item in successes} == {4, 5}
    assert len(failures) == 18
    assert all(type(item) is WarmupLimitExceededError for item in failures)
    assert await _stored_counts(sf, tenant, identity_id) == (5, 5, [1, 2, 3, 4, 5])
    assert len(audit.records) == 2


async def test_twenty_same_key_share_one_reservation_and_one_counter_increment(
    engine_fx: AsyncEngine,
) -> None:
    """同 key 并发重试都成功返回同一 immutable snapshot，但各自有 allow audit。"""
    tenant = TenantId("tReservationSameKey")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local="same",
        domain="cold.reservation-same.example",
    )
    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        action_before = (
            await session.execute(
                select(func.count(tables.IdentityActionRow.action_id)).where(
                    tables.IdentityActionRow.tenant_id == tenant
                )
            )
        ).scalar_one()
        outbox_before = (
            await session.execute(
                select(func.count(tables.OutboxEventRow.event_id)).where(
                    tables.OutboxEventRow.tenant_id == tenant
                )
            )
        ).scalar_one()
    audit.records.clear()

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                _attempt(service, tenant, identity_id, "same-key")
                for _ in range(20)
            ),
            return_exceptions=True,
        ),
        timeout=20,
    )

    assert all(isinstance(item, SendReservation) for item in results)
    reservations = [item for item in results if isinstance(item, SendReservation)]
    assert len({item.reservation_id for item in reservations}) == 1
    assert {
        (item.sequence, item.daily_limit, item.remaining_today)
        for item in reservations
    } == {(1, 5, 4)}
    assert await _stored_counts(sf, tenant, identity_id) == (1, 1, [1])
    assert len(audit.records) == 20
    async with sf() as session:
        assert (
            await session.execute(
                select(func.count(tables.IdentityActionRow.action_id)).where(
                    tables.IdentityActionRow.tenant_id == tenant
                )
            )
        ).scalar_one() == action_before
        assert (
            await session.execute(
                select(func.count(tables.OutboxEventRow.event_id)).where(
                    tables.OutboxEventRow.tenant_id == tenant
                )
            )
        ).scalar_one() == outbox_before


async def test_two_identities_on_one_domain_finish_without_deadlock_or_error_masking(
    engine_fx: AsyncEngine,
) -> None:
    """同域双 identity 并发遵循 domain-first 锁序并各自精确停在 cap。"""
    tenant = TenantId("tReservationSharedDomain")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    domain = "cold.reservation-shared.example"
    first = await _ready_identity(service, tenant, local="first", domain=domain)
    second = await _ready_identity(service, tenant, local="second", domain=domain)
    audit.records.clear()

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                _attempt(service, tenant, identity_id, f"{identity_id}-{index}")
                for identity_id in (first, second)
                for index in range(10)
            ),
            return_exceptions=True,
        ),
        timeout=25,
    )

    successes = [item for item in results if isinstance(item, SendReservation)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(successes) == 10
    assert len(failures) == 10
    assert all(type(item) is WarmupLimitExceededError for item in failures)
    assert await _stored_counts(sf, tenant, first) == (5, 5, [1, 2, 3, 4, 5])
    assert await _stored_counts(sf, tenant, second) == (5, 5, [1, 2, 3, 4, 5])
    assert len(audit.records) == 10


async def test_non_idempotency_unique_failure_rolls_back_and_is_not_mapped_to_cap(
    engine_fx: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 reservation-key unique 冲突必须保持 DB 异常且不增加 counter/reservation。"""
    tenant = TenantId("tReservationUniqueFailure")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit)
    identity_id = await _ready_identity(
        service,
        tenant,
        local="unique",
        domain="cold.reservation-unique.example",
    )
    existing = await service.reserve_send_slot(
        tenant,
        identity_id,
        IdempotencyKey("existing"),
        True,
        actor=_system(identity_id),
    )
    audit.records.clear()
    repository_module = importlib.import_module(
        "infra.db.repositories.sending_identities"
    )
    monkeypatch.setattr(repository_module, "new_id", lambda prefix: existing.reservation_id)

    with pytest.raises(IntegrityError):
        await service.reserve_send_slot(
            tenant,
            identity_id,
            IdempotencyKey("different-key"),
            True,
            actor=_system(identity_id),
        )

    assert await _stored_counts(sf, tenant, identity_id) == (1, 1, [1])
    assert audit.records == []


class _FailingCommitSession(AsyncSession):
    async def commit(self) -> None:
        await self.flush()
        raise RuntimeError("private reservation commit failure")


async def test_commit_failure_rolls_back_counter_and_reservation_with_zero_allow(
    engine_fx: AsyncEngine,
) -> None:
    """commit 在 flush 后失败也不能留下 counter/reservation 或 allow audit。"""
    tenant = TenantId("tReservationCommitFailure")
    normal_sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    seed_audit = _Audit()
    seed_service = _service(normal_sf, tenant, seed_audit)
    identity_id = await _ready_identity(
        seed_service,
        tenant,
        local="commit",
        domain="cold.reservation-commit.example",
    )
    assert await _stored_counts(normal_sf, tenant, identity_id) == (0, 0, [])

    failing_sf = async_sessionmaker(
        bind=engine_fx,
        expire_on_commit=False,
        class_=_FailingCommitSession,
    )
    audit = _Audit()
    failing_service = _service(failing_sf, tenant, audit)
    with pytest.raises(RuntimeError, match="private reservation commit failure"):
        await failing_service.reserve_send_slot(
            tenant,
            identity_id,
            IdempotencyKey("commit-failure"),
            True,
            actor=_system(identity_id),
        )

    assert await _stored_counts(normal_sf, tenant, identity_id) == (0, 0, [])
    assert audit.records == []


class _CorruptingIdentities:
    def __init__(self, delegate) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def get(self, tenant_id, identity_id, *, for_update=False):
        identity = await self._delegate.get(
            tenant_id, identity_id, for_update=for_update
        )
        if identity is not None and for_update:
            identity.tenant_id = TenantId("tCorrupted")
        return identity


class _CorruptingDomains:
    def __init__(self, delegate) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def ensure(self, domain: SendingDomain) -> SendingDomain:
        winner = await self._delegate.ensure(domain)
        return SendingDomain(
            TenantId("tCorrupted"), winner.domain, winner.role, winner.created_at
        )


class _CorruptingUow:
    def __init__(self, delegate, kind: str) -> None:
        self._delegate = delegate
        self._kind = kind

    async def __aenter__(self):
        entered = await self._delegate.__aenter__()
        self.__dict__.update(
            {
                name: getattr(entered, name)
                for name in (
                    "domains",
                    "identities",
                    "auth_checks",
                    "reputation",
                    "counters",
                    "reservations",
                    "actions",
                    "bus",
                )
            }
        )
        if self._kind == "domain":
            self.domains = _CorruptingDomains(self.domains)
        else:
            self.identities = _CorruptingIdentities(self.identities)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._delegate.__aexit__(exc_type, exc, tb)


@pytest.mark.parametrize("corruption", ["domain", "identity"])
async def test_corrupt_repository_rows_fail_closed_without_reservation_increment(
    engine_fx: AsyncEngine,
    corruption: str,
) -> None:
    """domain/identity tenant corruption 均在 atomic reserve 前拒绝并回滚。"""
    tenant = TenantId(f"tReservationCorruption{corruption.title()}")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    seed_audit = _Audit()
    seed_service = _service(sf, tenant, seed_audit)
    identity_id = await _ready_identity(
        seed_service,
        tenant,
        local=corruption,
        domain=f"cold.reservation-{corruption}.example",
    )
    audit = _Audit()
    service = _service(
        sf,
        tenant,
        audit,
        uow_wrapper=lambda uow: _CorruptingUow(uow, corruption),
    )

    with pytest.raises(TenantIsolationViolation):
        await service.reserve_send_slot(
            tenant,
            identity_id,
            IdempotencyKey(f"{corruption}-corruption"),
            True,
            actor=_system(identity_id),
        )

    assert await _stored_counts(sf, tenant, identity_id) == (0, 0, [])
    assert not any(str(record["rule"]).startswith("phase1:") for record in audit.records)
