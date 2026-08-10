"""发件身份 PostgreSQL repository/UoW 行为契约。"""
from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.sending_identity.permissions import (
    ScopeLevel,
    SendingIdentityAction,
    SendingIdentityScope,
)
from domains.sending_identity.schemas import (
    AuthenticationFailure,
    AuthenticationResult,
    DeliveryEventRecord,
)
from shared.errors import TenantIsolationViolation
from shared.events.catalog import SendingIdentityActivated
from shared.schemas.identifiers import (
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
    new_id,
)

_models = importlib.import_module("domains.sending_identity.models")
AuthCheck = _models.AuthCheck
AuthenticationFailureCategory = _models.AuthenticationFailureCategory
AuthenticationFixInstruction = _models.AuthenticationFixInstruction
DeliveryEventType = _models.DeliveryEventType
DomainRole = _models.DomainRole
IdentityState = _models.IdentityState
ReputationThresholds = _models.ReputationThresholds
SendingIdentity = _models.SendingIdentity
WarmupPlan = _models.WarmupPlan

_repository = importlib.import_module("domains.sending_identity.repository")
AuthenticationCheckRecord = _repository.AuthenticationCheckRecord
IdentityActionRecord = _repository.IdentityActionRecord
ReservationOutcome = _repository.ReservationOutcome
SendingDomain = _repository.SendingDomain

_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
_MODULE = "infra.db.repositories.sending_identities"


def _load(symbol: str):
    try:
        return getattr(importlib.import_module(_MODULE), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _row(symbol: str):
    try:
        return getattr(importlib.import_module("infra.db.tables"), symbol)
    except AttributeError as exc:
        pytest.fail(f"RED：ORM {symbol} 尚未创建（{exc}）")


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _identity(
    tenant: str,
    identity: str,
    domain: str,
    *,
    address: str | None = None,
    state: IdentityState = IdentityState.ACTIVE,
    created_at: datetime = _NOW,
    previous: IdentityState | None = None,
    category: object | None = None,
) -> SendingIdentity:
    kwargs = {"suspension_category": category} if category is not None else {}
    return SendingIdentity(
        identity_id=SendingIdentityId(identity),
        tenant_id=TenantId(tenant),
        address=address or f"{identity.lower()}@{domain}",
        domain=domain,
        role=DomainRole.COLD_OUTREACH,
        state=state,
        created_at=created_at,
        warmup_plan=WarmupPlan(date(2026, 7, 1), 80),
        thresholds=ReputationThresholds(
            throttle_hard_bounce_rate=Decimal("0.030000"),
            suspend_hard_bounce_rate=Decimal("0.050000"),
            throttle_complaint_rate=Decimal("0.001000"),
            suspend_complaint_rate=Decimal("0.003000"),
            minimum_sample=50,
        ),
        activated_at=_NOW if state is IdentityState.ACTIVE else None,
        connector_ref="gmail_primary",
        sendable_state_before_restriction=previous,
        **kwargs,
    )


def _auth(
    tenant: str,
    identity: str,
    auth_id: str,
    *,
    passed: bool = True,
    checked_at: datetime = _NOW,
) -> AuthenticationCheckRecord:
    failures = () if passed else (
        AuthenticationFailure(
            check=AuthCheck.SPF,
            category=AuthenticationFailureCategory.RECORD_MISSING,
            instruction=AuthenticationFixInstruction.CONFIGURE_SPF,
        ),
        AuthenticationFailure(
            check=AuthCheck.DKIM,
            category=AuthenticationFailureCategory.RECORD_MISSING,
            instruction=AuthenticationFixInstruction.CONFIGURE_DKIM,
        ),
        AuthenticationFailure(
            check=AuthCheck.DMARC,
            category=AuthenticationFailureCategory.RECORD_MISSING,
            instruction=AuthenticationFixInstruction.CONFIGURE_DMARC,
        ),
    )
    return AuthenticationCheckRecord(
        auth_check_id=auth_id,
        tenant_id=TenantId(tenant),
        identity_id=SendingIdentityId(identity),
        result=AuthenticationResult(
            checked_at=checked_at,
            spf_passed=passed,
            dkim_passed=passed,
            dmarc_passed=passed,
            failures=failures,
            check_ref=f"ref_{auth_id}",
        ),
        created_at=checked_at,
    )


def _unsafe_scope(level: ScopeLevel) -> SendingIdentityScope:
    scope = object.__new__(SendingIdentityScope)
    object.__setattr__(scope, "level", level)
    object.__setattr__(scope, "allowed_identity_ids", None)
    object.__setattr__(scope, "allowed_domains", None)
    return scope


def _unsafe_scope_with_ids(
    level: ScopeLevel, ids: frozenset[SendingIdentityId] | None
) -> SendingIdentityScope:
    scope = _unsafe_scope(level)
    object.__setattr__(scope, "allowed_identity_ids", ids)
    return scope


async def test_all_repositories_map_and_isolate_tenants(
    engine_fx: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """七类仓储映射真实行；错租户读为空、写拒绝且安全 CRITICAL 审计。"""
    tenant = TenantId("tRepoMap")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    session = sf()
    try:
        domains = _load("SendingDomainRepositoryImpl")(session, tenant)
        identities = _load("SendingIdentityRepositoryImpl")(session, tenant)
        auth = _load("AuthenticationCheckRepositoryImpl")(session, tenant)
        reputation = _load("ReputationRepositoryImpl")(session, tenant)
        counters = _load("SendCounterRepositoryImpl")(session, tenant)
        actions = _load("IdentityActionRepositoryImpl")(session, tenant)

        domain = SendingDomain(tenant, "cold.repo.example", DomainRole.COLD_OUTREACH, _NOW)
        identity = _identity(str(tenant), "sidRepoMap", domain.domain)
        await domains.add(domain)
        await identities.add(identity)
        await auth.add(_auth(str(tenant), "sidRepoMap", "authRepoMap"))
        event = DeliveryEventRecord(
            tenant_id=tenant,
            identity_id=SendingIdentityId("sidRepoMap"),
            event_type=DeliveryEventType.DELIVERED,
            occurred_at=_NOW,
            dedup_key=IdempotencyKey("delivery-repo-map"),
            source_ref="gmail_evt_1",
        )
        assert await reputation.record_event(event) is True
        assert await reputation.record_event(event) is False
        action = IdentityActionRecord(
            action_id="actRepoMap",
            tenant_id=tenant,
            identity_id=SendingIdentityId("sidRepoMap"),
            action_key="register-repo-map",
            action=SendingIdentityAction.IDENTITY_REGISTER,
            before_state=None,
            after_state=IdentityState.CREATED,
            actor_id="boss_1",
            scope="tenant",
            rule="phase1:boss:tenant:identity:register",
            note=None,
            occurred_at=_NOW,
        )
        await actions.add(action)
        await session.commit()

        assert await domains.get(tenant, domain.domain) == domain
        loaded = await identities.get(tenant, identity.identity_id)
        assert loaded == identity
        assert await identities.find_by_address(tenant, identity.address) == identity
        assert await identities.find_domain_role(tenant, domain.domain) is DomainRole.COLD_OUTREACH
        assert await identities.list_domain_for_update(tenant, domain.domain) == [identity]
        assert await auth.latest_for_identity(tenant, identity.identity_id) == _auth(str(tenant), "sidRepoMap", "authRepoMap")
        assert await actions.exists_by_key(tenant, identity.identity_id, action.action_key) is True
        assert await counters.get_count(tenant, identity.identity_id, _NOW.date()) == 0

        identity.display_name = "Updated"
        await identities.update(identity)
        await session.commit()
        assert (await identities.get(tenant, identity.identity_id)).display_name == "Updated"  # type: ignore[union-attr]

        other = TenantId("tRepoOther")
        assert await domains.get(other, domain.domain) is None
        assert await identities.get(other, identity.identity_id) is None
        assert await identities.find_by_address(other, identity.address) is None
        assert await identities.list_domain_for_update(other, domain.domain) == []
        assert await auth.latest_for_identity(other, identity.identity_id) is None
        assert await actions.exists_by_key(other, identity.identity_id, action.action_key) is False
        assert await counters.get_count(other, identity.identity_id, _NOW.date()) == 0

        caplog.clear()
        with caplog.at_level(logging.CRITICAL, logger="infra.db.sending_identity.security"), pytest.raises(
            TenantIsolationViolation
        ) as caught:
            await identities.add(_identity(str(other), "sidOther", "cold.other.example"))
        records = [record for record in caplog.records if record.getMessage() == "检测到跨租户数据隔离违规"]
        assert len(records) == 1
        assert set(records[0].__dict__) >= {"repository", "tenant_id", "rule"}
        rendered = caplog.text
        assert "cold.other.example" not in rendered
        assert "sidother@" not in rendered.lower()
        assert "gmail_primary" not in rendered
        assert caught.value.context == {
            "repository": "SendingIdentityRepositoryImpl",
            "tenant_id": "tRepoMap",
            "rule": "sending_identity_write_tenant",
        }
    finally:
        await session.close()


async def test_campaign_list_filters_latest_auth_scope_before_limit(engine_fx: AsyncEngine) -> None:
    """不合格早行不会吃掉 LIMIT；latest auth 用 auth_check_id 稳定决胜。"""
    tenant = TenantId("tRepoList")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    async with sf() as session:
        domains = _load("SendingDomainRepositoryImpl")(session, tenant)
        identities = _load("SendingIdentityRepositoryImpl")(session, tenant)
        auth = _load("AuthenticationCheckRepositoryImpl")(session, tenant)
        await domains.add(SendingDomain(tenant, "cold.list.example", DomainRole.COLD_OUTREACH, _NOW))
        for index in range(5):
            candidate = _identity(
                str(tenant),
                f"sidBad{index}",
                "cold.list.example",
                state=IdentityState.CREATED,
                created_at=_NOW - timedelta(days=10 - index),
            )
            candidate.warmup_plan = None
            candidate.activated_at = None
            await identities.add(candidate)
        for index in range(3):
            candidate = _identity(
                str(tenant), f"sidGood{index}", "cold.list.example", created_at=_NOW + timedelta(minutes=index)
            )
            await identities.add(candidate)
            await auth.add(_auth(str(tenant), str(candidate.identity_id), f"authGood{index}"))
        await session.commit()

        scope = SendingIdentityScope(level=ScopeLevel.TENANT)
        rows = await identities.list_available_for_campaign(tenant, scope, 2)
        assert [str(item.identity_id) for item in rows] == ["sidGood0", "sidGood1"]

        manager = SendingIdentityScope(
            level=ScopeLevel.MANAGER,
            allowed_identity_ids=frozenset({SendingIdentityId("sidGood2")}),
        )
        assert [str(item.identity_id) for item in await identities.list_available_for_campaign(tenant, manager, 5)] == ["sidGood2"]
        system_one = SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({SendingIdentityId("sidGood1")}),
        )
        assert [
            str(item.identity_id)
            for item in await identities.list_available_for_campaign(tenant, system_one, 5)
        ] == ["sidGood1"]
        assert await identities.list_available_for_campaign(
            tenant,
            SendingIdentityScope(level=ScopeLevel.SELF),
            5,
        ) == []
        assert await identities.list_available_for_campaign(
            tenant,
            SendingIdentityScope(
                level=ScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset(
                    {SendingIdentityId("sidGood0"), SendingIdentityId("sidGood1")}
                ),
            ),
            5,
        ) == []
        assert await identities.list_available_for_campaign(
            tenant, _unsafe_scope_with_ids(ScopeLevel.SYSTEM, frozenset()), 5
        ) == []
        assert await identities.list_available_for_campaign(TenantId("tWrong"), scope, 5) == []
        assert await identities.list_available_for_campaign(tenant, SendingIdentityScope(), 5) == []
        assert await identities.list_available_for_campaign(tenant, _unsafe_scope(ScopeLevel.SYSTEM), 5) == []
        assert await identities.list_available_for_campaign(tenant, _unsafe_scope(ScopeLevel.MANAGER), 5) == []

        checked = _NOW + timedelta(hours=1)
        await auth.add(_auth(str(tenant), "sidGood2", "authTieA", passed=True, checked_at=checked))
        await auth.add(_auth(str(tenant), "sidGood2", "authTieB", passed=False, checked_at=checked))
        await session.commit()
        assert await identities.list_available_for_campaign(tenant, manager, 5) == []


async def test_reservation_is_atomic_idempotent_and_rolls_back_other_integrity_errors(
    engine_fx: AsyncEngine,
) -> None:
    """reservation typed 三态、显式时间、counter 同事务及非目标冲突回滚。"""
    tenant = TenantId("tReserve")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    async with sf() as session:
        domains = _load("SendingDomainRepositoryImpl")(session, tenant)
        identities = _load("SendingIdentityRepositoryImpl")(session, tenant)
        reservations = _load("SendReservationRepositoryImpl")(session, tenant)
        counters = _load("SendCounterRepositoryImpl")(session, tenant)
        await domains.add(SendingDomain(tenant, "cold.reserve.example", DomainRole.COLD_OUTREACH, _NOW))
        identity = _identity(str(tenant), "sidReserve", "cold.reserve.example")
        await identities.add(identity)
        await session.commit()

        created = await reservations.reserve_if_below(
            tenant, identity.identity_id, IdempotencyKey("reserve-key-1"), _NOW.date(), 1, _NOW
        )
        assert created.outcome is ReservationOutcome.CREATED
        assert created.reservation is not None
        assert created.reservation.sequence == 1
        assert created.reservation.remaining_today == 0
        assert await counters.get_count(tenant, identity.identity_id, _NOW.date()) == 1
        await session.commit()

        existing = await reservations.reserve_if_below(
            tenant, identity.identity_id, IdempotencyKey("reserve-key-1"), _NOW.date(), 1, _NOW + timedelta(hours=1)
        )
        assert existing.outcome is ReservationOutcome.EXISTING
        assert existing.reservation == created.reservation
        capped = await reservations.reserve_if_below(
            tenant, identity.identity_id, IdempotencyKey("reserve-key-2"), _NOW.date(), 1, _NOW
        )
        assert capped.outcome is ReservationOutcome.CAP_REACHED
        assert capped.reservation is None
        assert capped.sent_attempts == 1
        await session.commit()

        reservation_row = _row("SendReservationRow")
        stored_at = (
            await session.execute(
                select(reservation_row.created_at).where(reservation_row.tenant_id == tenant)
            )
        ).scalar_one()
        assert stored_at == _NOW

    async with sf() as session:
        reservations = _load("SendReservationRepositoryImpl")(session, tenant)
        with pytest.raises(TenantIsolationViolation):
            await reservations.reserve_if_below(
                TenantId("tWrong"), SendingIdentityId("sidReserve"), IdempotencyKey("wrong"), _NOW.date(), 2, _NOW
            )
        with pytest.raises(IntegrityError):
            await reservations.reserve_if_below(
                tenant, SendingIdentityId("missingIdentity"), IdempotencyKey("fk-error"), _NOW.date(), 2, _NOW
            )
        await session.rollback()
        counter_row = _row("SendCounterRow")
        count = (
            await session.execute(
                select(counter_row).where(
                    counter_row.tenant_id == tenant,
                    counter_row.identity_id == "missingIdentity",
                )
            )
        ).scalars().all()
        assert count == []


async def test_suspended_identity_category_roundtrips_on_add_and_update(
    engine_fx: AsyncEngine,
) -> None:
    """typed suspension category 在 add/get/update 中不丢失且可安全清除。"""
    category = _models.SuspensionCategory
    tenant = TenantId("tRepoCategory")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    async with sf() as session:
        domains = _load("SendingDomainRepositoryImpl")(session, tenant)
        identities = _load("SendingIdentityRepositoryImpl")(session, tenant)
        await domains.add(
            SendingDomain(
                tenant, "cold.category.example", DomainRole.COLD_OUTREACH, _NOW
            )
        )
        identity = _identity(
            str(tenant),
            "sidCategory",
            "cold.category.example",
            state=IdentityState.SUSPENDED,
            previous=IdentityState.ACTIVE,
            category=category.SPAM_TRAP,
        )
        await identities.add(identity)
        await session.commit()
        loaded = await identities.get(tenant, identity.identity_id)
        assert loaded == identity
        assert loaded.suspension_category is category.SPAM_TRAP

        loaded.transition_to(IdentityState.ACTIVE)
        await identities.update(loaded)
        await session.commit()
        restored = await identities.get(tenant, identity.identity_id)
        assert restored is not None
        assert restored.state is IdentityState.ACTIVE
        assert restored.suspension_category is None


async def test_reputation_windows_use_immutable_reservation_timestamps_and_exact_bounds(
    engine_fx: AsyncEngine,
) -> None:
    """身份/域窗口包含双端边界，排除前后 1μs，并隔离同名跨租户域。"""
    tenant = TenantId("tWindow")
    other = TenantId("tWindowOther")
    domain = "cold.window.example"
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    async with sf() as session:
        DomainRow = _row("SendingDomainRow")
        ReservationRow = _row("SendReservationRow")
        ReputationRow = _row("ReputationEventRow")
        for item_tenant, identity in ((tenant, "sidWindow"), (other, "sidOtherWindow")):
            session.add(DomainRow(tenant_id=item_tenant, domain=domain, role="cold_outreach", created_at=_NOW))
            model = _identity(str(item_tenant), identity, domain)
            repo = _load("SendingIdentityRepositoryImpl")(session, item_tenant)
            await repo.add(model)
        await session.flush()
        start = _NOW - timedelta(days=7)
        for index, at in enumerate((start - timedelta(microseconds=1), start, _NOW, _NOW + timedelta(microseconds=1))):
            session.add(ReservationRow(tenant_id=tenant, reservation_id=f"resWin{index}", identity_id="sidWindow", reservation_key=f"keyWin{index}", on_day=at.date(), sequence=index + 1, created_at=at))
        events = (
            ("repBefore", start - timedelta(microseconds=1), "delivered"),
            ("repStart", start, "delivered"),
            ("repEnd", _NOW, "hard_bounced"),
            ("repAfter", _NOW + timedelta(microseconds=1), "complaint"),
        )
        for event_id, at, kind in events:
            session.add(ReputationRow(tenant_id=tenant, reputation_event_id=event_id, identity_id="sidWindow", event_type=kind, occurred_at=at, dedup_key=f"dedup-{event_id}", source_ref=f"ref_{event_id}", created_at=at))
        session.add(ReservationRow(tenant_id=other, reservation_id="resOther", identity_id="sidOtherWindow", reservation_key="keyOther", on_day=_NOW.date(), sequence=1, created_at=_NOW))
        session.add(ReputationRow(tenant_id=other, reputation_event_id="repOther", identity_id="sidOtherWindow", event_type="complaint", occurred_at=_NOW, dedup_key="dedup-other", source_ref="ref_other", created_at=_NOW))
        await session.commit()

        reputation = _load("ReputationRepositoryImpl")(session, tenant)
        identity_window = await reputation.compute_window(tenant, SendingIdentityId("sidWindow"), 7, _NOW)
        assert (identity_window.sent_attempts, identity_window.delivered, identity_window.hard_bounced, identity_window.complaints) == (2, 1, 1, 0)
        domain_window = await reputation.compute_domain_window(tenant, domain, 7, _NOW)
        assert domain_window == identity_window
        wrong = await reputation.compute_window(other, SendingIdentityId("sidWindow"), 7, _NOW)
        assert wrong.sent_attempts == 0


async def test_sending_identity_uow_commits_rolls_back_and_uses_fresh_sessions(
    engine_fx: AsyncEngine,
) -> None:
    """UoW 七 repo 与 bus 同 session；正常提交、异常回滚、每次 enter 新 session。"""
    try:
        Uow = importlib.import_module(
            "infra.db.sending_identity_uow"
        ).SqlAlchemySendingIdentityUnitOfWork
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：SqlAlchemySendingIdentityUnitOfWork 尚未创建（{exc}）")
    tenant = TenantId("tUowSending")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    sessions: list[object] = []
    async with Uow(sf, tenant, now=lambda: _NOW) as uow:
        sessions.append(uow._session)
        assert all(
            getattr(uow, name)._session is uow._session
            for name in ("domains", "identities", "auth_checks", "reputation", "counters", "reservations", "actions")
        )
        assert uow.bus._session is uow._session
        await uow.domains.add(SendingDomain(tenant, "cold.uow.example", DomainRole.COLD_OUTREACH, _NOW))
    async with Uow(sf, tenant, now=lambda: _NOW) as uow:
        sessions.append(uow._session)
        assert await uow.domains.get(tenant, "cold.uow.example") is not None
    assert sessions[0] is not sessions[1]

    with pytest.raises(RuntimeError):
        async with Uow(sf, tenant, now=lambda: _NOW) as uow:
            await uow.domains.add(SendingDomain(tenant, "cold.rollback.example", DomainRole.COLD_OUTREACH, _NOW))
            raise RuntimeError("rollback")
    async with sf() as verify:
        DomainRow = _row("SendingDomainRow")
        assert (
            await verify.execute(select(DomainRow).where(DomainRow.domain == "cold.rollback.example"))
        ).scalars().all() == []


class _FailingCommitSession(AsyncSession):
    rollback_attempts = 0

    async def commit(self) -> None:
        await self.flush()
        raise RuntimeError("primary commit failure")

    async def rollback(self) -> None:
        type(self).rollback_attempts += 1
        await super().rollback()
        raise RuntimeError("secondary rollback failure")


async def test_sending_identity_uow_commit_failure_rolls_back_everything(
    engine_fx: AsyncEngine,
) -> None:
    """commit 失败显式 rollback；rollback 自身失败不覆盖原错，四类行均不存在。"""
    Uow = importlib.import_module(
        "infra.db.sending_identity_uow"
    ).SqlAlchemySendingIdentityUnitOfWork
    tenant = TenantId("tUowCommitFail")
    failing_sf = async_sessionmaker(
        bind=engine_fx, expire_on_commit=False, class_=_FailingCommitSession
    )
    _FailingCommitSession.rollback_attempts = 0
    with pytest.raises(RuntimeError, match="primary commit failure"):
        async with Uow(failing_sf, tenant, now=lambda: _NOW) as uow:
            domain = SendingDomain(
                tenant, "cold.commit-fail.example", DomainRole.COLD_OUTREACH, _NOW
            )
            identity = _identity(
                str(tenant),
                new_id("sid"),
                domain.domain,
                state=IdentityState.ACTIVE,
            )
            await uow.domains.add(domain)
            await uow.identities.add(identity)
            await uow.actions.add(
                IdentityActionRecord(
                    action_id="actCommitFail",
                    tenant_id=tenant,
                    identity_id=identity.identity_id,
                    action_key="commit-failure",
                    action=SendingIdentityAction.IDENTITY_REGISTER,
                    before_state=None,
                    after_state=IdentityState.ACTIVE,
                    actor_id="boss_1",
                    scope="tenant",
                    rule="phase1:boss:tenant:identity:register",
                    note=None,
                    occurred_at=_NOW,
                )
            )
            await uow.bus.publish(
                SendingIdentityActivated(
                    tenant_id=tenant,
                    occurred_at=_NOW,
                    run_id=None,
                    sending_identity_id=identity.identity_id,
                )
            )
    assert _FailingCommitSession.rollback_attempts == 1

    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    async with sf() as verify:
        for row_type in (
            _row("SendingDomainRow"),
            _row("SendingIdentityRow"),
            _row("IdentityActionRow"),
            _row("OutboxEventRow"),
        ):
            rows = (
                await verify.execute(
                    select(row_type).where(row_type.tenant_id == tenant)
                )
            ).scalars().all()
            assert rows == []


_COMMIT_PRIMARY = RuntimeError("commit primary must remain private")
_ROLLBACK_CANCEL = asyncio.CancelledError("rollback cancellation must remain private")
_CLOSE_CANCEL = asyncio.CancelledError("close cancellation must remain private")
_BODY_CANCEL = asyncio.CancelledError("body cancellation must remain private")


class _CommitRollbackCancelledSession(AsyncSession):
    async def commit(self) -> None:
        raise _COMMIT_PRIMARY

    async def rollback(self) -> None:
        raise _ROLLBACK_CANCEL


class _CommitCloseCancelledSession(AsyncSession):
    async def commit(self) -> None:
        raise _COMMIT_PRIMARY

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        raise _CLOSE_CANCEL


class _CloseCancelledSession(AsyncSession):
    async def commit(self) -> None:
        return None

    async def close(self) -> None:
        raise _CLOSE_CANCEL


class _BodyCleanupCancelledSession(AsyncSession):
    async def rollback(self) -> None:
        raise _ROLLBACK_CANCEL

    async def close(self) -> None:
        raise _CLOSE_CANCEL


def _uow_factory(session_type: type[AsyncSession]):
    return async_sessionmaker(expire_on_commit=False, class_=session_type)


async def test_uow_rollback_cancelled_error_does_not_replace_commit_primary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """commit 已失败时 rollback cancellation 仅脱敏记录，原对象必须继续传播。"""
    Uow = importlib.import_module(
        "infra.db.sending_identity_uow"
    ).SqlAlchemySendingIdentityUnitOfWork
    with (
        caplog.at_level(logging.ERROR, logger="infra.db.sending_identity.uow"),
        pytest.raises(RuntimeError) as caught,
    ):
        async with Uow(
            _uow_factory(_CommitRollbackCancelledSession), TenantId("tUowCancel")
        ):
            pass
    assert caught.value is _COMMIT_PRIMARY
    assert [record.getMessage() for record in caplog.records] == ["发件身份事务回滚失败"]
    assert "private" not in caplog.text


async def test_uow_close_cancelled_error_does_not_replace_commit_primary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """commit 已失败时 close cancellation 仅脱敏记录，原对象必须继续传播。"""
    Uow = importlib.import_module(
        "infra.db.sending_identity_uow"
    ).SqlAlchemySendingIdentityUnitOfWork
    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="infra.db.sending_identity.uow"),
        pytest.raises(RuntimeError) as caught,
    ):
        async with Uow(
            _uow_factory(_CommitCloseCancelledSession), TenantId("tUowCancel")
        ):
            pass
    assert caught.value is _COMMIT_PRIMARY
    assert [record.getMessage() for record in caplog.records] == ["发件身份事务关闭失败"]
    assert "private" not in caplog.text


async def test_uow_close_cancelled_error_propagates_without_primary() -> None:
    """正常 commit 后 close cancellation 没有主异常可保护，必须原对象传播。"""
    Uow = importlib.import_module(
        "infra.db.sending_identity_uow"
    ).SqlAlchemySendingIdentityUnitOfWork
    with pytest.raises(asyncio.CancelledError) as caught:
        async with Uow(
            _uow_factory(_CloseCancelledSession), TenantId("tUowCancel")
        ):
            pass
    assert caught.value is _CLOSE_CANCEL


async def test_uow_body_cancelled_error_survives_cancelled_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """with-body cancellation 本身是 primary，rollback/close cancellation 都不得替换。"""
    Uow = importlib.import_module(
        "infra.db.sending_identity_uow"
    ).SqlAlchemySendingIdentityUnitOfWork
    caplog.clear()
    with (
        caplog.at_level(logging.ERROR, logger="infra.db.sending_identity.uow"),
        pytest.raises(asyncio.CancelledError) as caught,
    ):
        async with Uow(
            _uow_factory(_BodyCleanupCancelledSession), TenantId("tUowCancel")
        ):
            raise _BODY_CANCEL
    assert caught.value is _BODY_CANCEL
    assert [record.getMessage() for record in caplog.records] == [
        "发件身份事务回滚失败",
        "发件身份事务关闭失败",
    ]
    assert "private" not in caplog.text
