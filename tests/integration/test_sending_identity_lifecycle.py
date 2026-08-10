"""发件身份 lifecycle service 的真实 PostgreSQL 事务与并发证明。"""
from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.sending_identity.errors import DomainRoleConflictError
from domains.sending_identity.permissions import (
    Actor,
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityScope,
)
from domains.sending_identity.schemas import (
    AuthenticationFailure,
    AuthenticationResult,
    IdentityRegisterRequest,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import SendingIdentityId, TenantId

_models = importlib.import_module("domains.sending_identity.models")
AuthCheck = _models.AuthCheck
AuthenticationFailureCategory = _models.AuthenticationFailureCategory
AuthenticationFixInstruction = _models.AuthenticationFixInstruction
DomainRole = _models.DomainRole
IdentityState = _models.IdentityState
SuspensionCategory = _models.SuspensionCategory

_NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _service_class():
    try:
        return importlib.import_module(
            "domains.sending_identity.service_impl"
        ).SendingIdentityServiceImpl
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：SendingIdentityServiceImpl 尚未创建（{exc}）")


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def log(self, **record) -> None:
        self.records.append(record)


def _boss() -> Actor:
    return Actor(
        actor_id="boss_pg",
        role="boss",
        scope=SendingIdentityScope(level=ScopeLevel.TENANT),
    )


def _system(identity_id: SendingIdentityId) -> Actor:
    return Actor(
        actor_id="system_pg",
        role="system",
        scope=SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
    )


def _auth(
    checked_at: datetime,
    *,
    passed: bool = True,
    check_ref: str = "dns_pg_1",
) -> AuthenticationResult:
    failures = ()
    if not passed:
        failures = (
            AuthenticationFailure(
                check=AuthCheck.SPF,
                category=AuthenticationFailureCategory.RECORD_MISSING,
                instruction=AuthenticationFixInstruction.CONFIGURE_SPF,
            ),
        )
    return AuthenticationResult(
        checked_at=checked_at,
        spf_passed=passed,
        dkim_passed=True,
        dmarc_passed=True,
        failures=failures,
        check_ref=check_ref,
    )


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    audit: _Audit,
    now: datetime,
):
    Uow = importlib.import_module(
        "infra.db.sending_identity_uow"
    ).SqlAlchemySendingIdentityUnitOfWork
    return _service_class()(
        lambda requested_tenant: Uow(
            session_factory, requested_tenant, now=lambda: now
        ),
        Phase1SendingIdentityAuthorizer(tenant),
        audit,
        now=lambda: now,
    )


async def test_real_lifecycle_commits_state_auth_actions_and_outbox_atomically(
    engine_fx: AsyncEngine,
) -> None:
    """认证不自动预热；day29 activation 的 state/action/outbox 同事务且只一次。"""
    tenant = TenantId("tLifecyclePg")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit, _NOW)
    request = IdentityRegisterRequest(
        address="sales@cold.lifecycle-pg.example",
        domain="cold.lifecycle-pg.example",
        role=DomainRole.COLD_OUTREACH,
        connector_ref="gmail_lifecycle",
    )
    identity_id = await service.register(tenant, request, actor=_boss())
    await service.begin_authentication(tenant, identity_id, actor=_boss())
    await service.record_authentication_result(
        tenant, identity_id, _auth(_NOW), actor=_system(identity_id)
    )
    before_warmup = await service.get(tenant, identity_id, actor=_boss())
    assert before_warmup.state is IdentityState.AUTH_PENDING
    await service.start_warmup(tenant, identity_id, 100, actor=_boss())

    day28 = _service(sf, tenant, audit, _NOW + timedelta(days=27))
    await day28.advance_warmup(tenant, identity_id, actor=_system(identity_id))
    assert (await day28.get(tenant, identity_id, actor=_boss())).state is IdentityState.WARMING
    day29 = _service(sf, tenant, audit, _NOW + timedelta(days=28))
    await asyncio.gather(
        *(
            day29.advance_warmup(tenant, identity_id, actor=_system(identity_id))
            for _ in range(5)
        )
    )
    assert (await day29.get(tenant, identity_id, actor=_boss())).state is IdentityState.ACTIVE

    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        actions = (
            await session.execute(
                select(tables.IdentityActionRow)
                .where(
                    tables.IdentityActionRow.tenant_id == tenant,
                    tables.IdentityActionRow.identity_id == identity_id,
                )
                .order_by(tables.IdentityActionRow.occurred_at)
            )
        ).scalars().all()
        assert [row.action_key for row in actions] == [
            f"identity:{identity_id}:v0:identity:register",
            f"identity:{identity_id}:v1:auth:check_begin",
            f"identity:{identity_id}:v2:warmup:start",
            f"identity:{identity_id}:v3:warmup:advance",
        ]
        assert (
            await session.execute(
                select(func.count(tables.AuthenticationCheckRow.auth_check_id)).where(
                    tables.AuthenticationCheckRow.tenant_id == tenant
                )
            )
        ).scalar_one() == 1
        outbox = (
            await session.execute(
                select(tables.OutboxEventRow).where(
                    tables.OutboxEventRow.tenant_id == tenant
                )
            )
        ).scalars().all()
        assert [row.event_type for row in outbox] == ["SendingIdentityActivated"]
    assert len(audit.records) == 13


async def test_concurrent_registration_recovers_exact_domain_and_address_winners(
    engine_fx: AsyncEngine,
) -> None:
    """并发 same-role/domain 与 duplicate address 幂等；异 role 恰一方成功。"""
    tenant = TenantId("tRegisterRacePg")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit, _NOW)
    same = IdentityRegisterRequest(
        address="same@cold.register-race.example",
        domain="cold.register-race.example",
        role=DomainRole.COLD_OUTREACH,
        display_name="Same",
    )
    ids = await asyncio.gather(
        *(service.register(tenant, same, actor=_boss()) for _ in range(12))
    )
    assert len(set(ids)) == 1

    distinct_same_role = await asyncio.gather(
        *(
            service.register(
                tenant,
                IdentityRegisterRequest(
                    address=f"distinct-{index}@cold.register-race.example",
                    domain="cold.register-race.example",
                    role=DomainRole.COLD_OUTREACH,
                ),
                actor=_boss(),
            )
            for index in range(2)
        )
    )
    assert len(set(distinct_same_role)) == 2

    async def register_conflicting_safe_field(display_name: str):
        try:
            return await service.register(
                tenant,
                IdentityRegisterRequest(
                    address="safe-conflict@cold.register-race.example",
                    domain="cold.register-race.example",
                    role=DomainRole.COLD_OUTREACH,
                    display_name=display_name,
                ),
                actor=_boss(),
            )
        except ValidationError as exc:
            return exc

    safe_field_results = await asyncio.gather(
        register_conflicting_safe_field("Winner A"),
        register_conflicting_safe_field("Winner B"),
    )
    assert sum(isinstance(item, str) for item in safe_field_results) == 1
    conflicts = [item for item in safe_field_results if isinstance(item, ValidationError)]
    assert len(conflicts) == 1
    assert str(conflicts[0]) == "发件身份登记冲突"

    async def register_role(role: DomainRole, local: str):
        try:
            return await service.register(
                tenant,
                IdentityRegisterRequest(
                    address=f"{local}@cold.role-race.example",
                    domain="cold.role-race.example",
                    role=role,
                ),
                actor=_boss(),
            )
        except DomainRoleConflictError:
            return None

    winners = await asyncio.gather(
        register_role(DomainRole.COLD_OUTREACH, "cold"),
        register_role(DomainRole.PRIMARY_BUSINESS, "primary"),
    )
    assert sum(item is not None for item in winners) == 1

    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        assert (
            await session.execute(
                select(func.count(tables.SendingIdentityRow.identity_id)).where(
                    tables.SendingIdentityRow.tenant_id == tenant,
                    tables.SendingIdentityRow.address == same.address,
                )
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                select(func.count(tables.IdentityActionRow.action_id)).where(
                    tables.IdentityActionRow.tenant_id == tenant,
                    tables.IdentityActionRow.identity_id == ids[0],
                )
            )
        ).scalar_one() == 1


async def test_concurrent_auth_refs_preserve_both_distinct_rows_and_one_typed_winner(
    engine_fx: AsyncEngine,
) -> None:
    """错误 row lock、比较符或 conflict target 会丢 distinct ref 或接受异值 same-ref。"""
    tenant = TenantId("tAuthRefRacePg")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit, _NOW)
    identity_id = await service.register(
        tenant,
        IdentityRegisterRequest(
            address="sales@cold.auth-ref-race.example",
            domain="cold.auth-ref-race.example",
            role=DomainRole.COLD_OUTREACH,
        ),
        actor=_boss(),
    )
    await service.begin_authentication(tenant, identity_id, actor=_boss())
    system = _system(identity_id)
    await asyncio.gather(
        service.record_authentication_result(
            tenant,
            identity_id,
            _auth(_NOW, check_ref="distinct_ref_a"),
            actor=system,
        ),
        service.record_authentication_result(
            tenant,
            identity_id,
            _auth(_NOW + timedelta(microseconds=1), check_ref="distinct_ref_b"),
            actor=system,
        ),
    )

    async def record_conflicting_ref(passed: bool):
        try:
            await service.record_authentication_result(
                tenant,
                identity_id,
                _auth(
                    _NOW + timedelta(microseconds=2),
                    passed=passed,
                    check_ref="same_ref_different_value",
                ),
                actor=system,
            )
            return "winner"
        except ValidationError as exc:
            return exc

    same_ref_results = await asyncio.gather(
        record_conflicting_ref(True),
        record_conflicting_ref(False),
    )
    assert same_ref_results.count("winner") == 1
    conflicts = [item for item in same_ref_results if isinstance(item, ValidationError)]
    assert len(conflicts) == 1
    assert str(conflicts[0]) == "认证检查引用冲突"

    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        refs = (
            await session.execute(
                select(tables.AuthenticationCheckRow.check_ref)
                .where(
                    tables.AuthenticationCheckRow.tenant_id == tenant,
                    tables.AuthenticationCheckRow.identity_id == identity_id,
                )
                .order_by(tables.AuthenticationCheckRow.check_ref)
            )
        ).scalars().all()
    assert refs == ["distinct_ref_a", "distinct_ref_b", "same_ref_different_value"]


async def test_concurrent_auth_regression_suspends_once_with_typed_category(
    engine_fx: AsyncEngine,
) -> None:
    """并发相同 auth ref 只追加一次，认证退化仅 action/outbox 一次。"""
    tenant = TenantId("tAuthRacePg")
    sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    audit = _Audit()
    service = _service(sf, tenant, audit, _NOW)
    identity_id = await service.register(
        tenant,
        IdentityRegisterRequest(
            address="sales@cold.auth-race.example",
            domain="cold.auth-race.example",
            role=DomainRole.COLD_OUTREACH,
        ),
        actor=_boss(),
    )
    await service.begin_authentication(tenant, identity_id, actor=_boss())
    await service.record_authentication_result(
        tenant,
        identity_id,
        _auth(_NOW, check_ref="auth_initial"),
        actor=_system(identity_id),
    )
    await service.start_warmup(tenant, identity_id, 50, actor=_boss())
    regression = _auth(
        _NOW + timedelta(minutes=1),
        passed=False,
        check_ref="auth_regression_same_ref",
    )
    await asyncio.gather(
        *(
            service.record_authentication_result(
                tenant, identity_id, regression, actor=_system(identity_id)
            )
            for _ in range(8)
        )
    )
    view = await service.get(tenant, identity_id, actor=_boss())
    assert view.state is IdentityState.SUSPENDED

    tables = importlib.import_module("infra.db.tables")
    async with sf() as session:
        row = (
            await session.execute(
                select(tables.SendingIdentityRow).where(
                    tables.SendingIdentityRow.tenant_id == tenant,
                    tables.SendingIdentityRow.identity_id == identity_id,
                )
            )
        ).scalar_one()
        assert row.suspension_category == SuspensionCategory.AUTHENTICATION_REGRESSION.value
        assert row.sendable_state_before_restriction == IdentityState.WARMING.value
        assert (
            await session.execute(
                select(func.count(tables.AuthenticationCheckRow.auth_check_id)).where(
                    tables.AuthenticationCheckRow.tenant_id == tenant,
                    tables.AuthenticationCheckRow.identity_id == identity_id,
                )
            )
        ).scalar_one() == 2
        suspended_events = (
            await session.execute(
                select(tables.OutboxEventRow).where(
                    tables.OutboxEventRow.tenant_id == tenant,
                    tables.OutboxEventRow.event_type == "SendingIdentitySuspended",
                )
            )
        ).scalars().all()
        assert len(suspended_events) == 1


class _FailingCommitSession(AsyncSession):
    async def commit(self) -> None:
        await self.flush()
        raise RuntimeError("private commit failure")


async def test_commit_failure_rolls_back_auth_state_action_outbox_and_has_zero_allow(
    engine_fx: AsyncEngine,
) -> None:
    """人为 commit failure 后四类写入全无，且不能提前写 allow audit。"""
    tenant = TenantId("tLifecycleCommitFail")
    normal_sf = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    seed_audit = _Audit()
    seed_service = _service(normal_sf, tenant, seed_audit, _NOW)
    identity_id = await seed_service.register(
        tenant,
        IdentityRegisterRequest(
            address="sales@cold.commit-lifecycle.example",
            domain="cold.commit-lifecycle.example",
            role=DomainRole.COLD_OUTREACH,
        ),
        actor=_boss(),
    )
    await seed_service.begin_authentication(tenant, identity_id, actor=_boss())
    await seed_service.record_authentication_result(
        tenant,
        identity_id,
        _auth(_NOW, check_ref="commit_seed"),
        actor=_system(identity_id),
    )
    await seed_service.start_warmup(tenant, identity_id, 50, actor=_boss())

    tables = importlib.import_module("infra.db.tables")
    async with normal_sf() as session:
        baseline_action_keys = (
            await session.execute(
                select(tables.IdentityActionRow.action_key)
                .where(
                    tables.IdentityActionRow.tenant_id == tenant,
                    tables.IdentityActionRow.identity_id == identity_id,
                )
                .order_by(tables.IdentityActionRow.action_key)
            )
        ).scalars().all()
    assert baseline_action_keys == sorted(
        [
            f"identity:{identity_id}:v0:identity:register",
            f"identity:{identity_id}:v1:auth:check_begin",
            f"identity:{identity_id}:v2:warmup:start",
        ]
    )

    failing_sf = async_sessionmaker(
        bind=engine_fx,
        expire_on_commit=False,
        class_=_FailingCommitSession,
    )
    failing_audit = _Audit()
    failing_service = _service(failing_sf, tenant, failing_audit, _NOW)
    with pytest.raises(RuntimeError, match="private commit failure"):
        await failing_service.record_authentication_result(
            tenant,
            identity_id,
            _auth(
                _NOW + timedelta(minutes=1),
                passed=False,
                check_ref="commit_regression",
            ),
            actor=_system(identity_id),
        )
    assert failing_audit.records == []

    async with normal_sf() as session:
        row = (
            await session.execute(
                select(tables.SendingIdentityRow).where(
                    tables.SendingIdentityRow.tenant_id == tenant,
                    tables.SendingIdentityRow.identity_id == identity_id,
                )
            )
        ).scalar_one()
        assert row.state == IdentityState.WARMING.value
        assert (
            await session.execute(
                select(func.count(tables.AuthenticationCheckRow.auth_check_id)).where(
                    tables.AuthenticationCheckRow.tenant_id == tenant,
                    tables.AuthenticationCheckRow.identity_id == identity_id,
                )
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                select(func.count(tables.OutboxEventRow.event_id)).where(
                    tables.OutboxEventRow.tenant_id == tenant
                )
            )
        ).scalar_one() == 0
        action_keys_after_failure = (
            await session.execute(
                select(tables.IdentityActionRow.action_key)
                .where(
                    tables.IdentityActionRow.tenant_id == tenant,
                    tables.IdentityActionRow.identity_id == identity_id,
                )
                .order_by(tables.IdentityActionRow.action_key)
            )
        ).scalars().all()
        assert action_keys_after_failure == baseline_action_keys
