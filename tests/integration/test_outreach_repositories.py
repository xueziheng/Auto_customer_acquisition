"""触达域 PostgreSQL repositories/UoW 原子、租户与回滚契约。"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    MessageId,
    ProspectAccountId,
    SendingIdentityId,
    SuppressionId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 11, 5, 0, tzinfo=UTC)
TENANT_A = TenantId(new_id("tn"))
TENANT_B = TenantId(new_id("tn"))
CAMPAIGN = CampaignId(new_id("cmp"))
ACCOUNT = ProspectAccountId(new_id("acc"))
CONTACT = ContactPointId(new_id("cp"))
SENDER = SendingIdentityId(new_id("sid"))


def _load(module: str, symbol: str) -> object:
    try:
        return getattr(importlib.import_module(module), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"缺少触达持久化 {module}.{symbol}: {exc}")


@pytest_asyncio.fixture
async def outreach_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    create_engine_from = _load("infra.db.session", "create_engine_from")
    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def outreach_factory(
    outreach_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(outreach_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _unique_outreach_namespace() -> None:
    """每例使用独立租户与业务主键，append-only 数据无需测试清理。"""
    global TENANT_A, TENANT_B, CAMPAIGN, ACCOUNT, CONTACT, SENDER
    TENANT_A = TenantId(new_id("tn"))
    TENANT_B = TenantId(new_id("tn"))
    CAMPAIGN = CampaignId(new_id("cmp"))
    ACCOUNT = ProspectAccountId(new_id("acc"))
    CONTACT = ContactPointId(new_id("cp"))
    SENDER = SendingIdentityId(new_id("sid"))


def _boundary() -> object:
    models = importlib.import_module("domains.outreach.models")
    return models.CampaignBoundary(
        markets=["US"],
        target_entity_types=["importer"],
        allowed_categories=["hardware"],
        sender_identity_ids=[SENDER],
        steps=[models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0)],
        daily_new_contact_limit=2,
        daily_total_message_limit=3,
        handoff_triggers=["quote_requested"],
        stop_on_reply=True,
    )


def _campaign_graph(
    tenant_id: TenantId | None = None,
    campaign_id: CampaignId | None = None,
) -> tuple[object, object]:
    models = importlib.import_module("domains.outreach.models")
    tenant_id = tenant_id or TENANT_A
    campaign_id = campaign_id or CAMPAIGN
    actor = EmployeeId(new_id("emp"))
    return (
        models.Campaign(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            state=models.CampaignState.DRAFT,
            current_version=1,
            created_by=actor,
            created_at=NOW,
        ),
        models.CampaignVersion(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            version=1,
            name="Hardware discovery",
            boundary=_boundary(),
            created_by=actor,
            created_at=NOW,
        ),
    )


def _enrollment(
    *,
    tenant_id: TenantId | None = None,
    enrollment_id: EnrollmentId | None = None,
    account_id: ProspectAccountId | None = None,
    key: str = "enrollment-key-1",
) -> object:
    models = importlib.import_module("domains.outreach.models")
    tenant_id = tenant_id or TENANT_A
    account_id = account_id or ACCOUNT
    return models.Enrollment(
        tenant_id=tenant_id,
        enrollment_id=enrollment_id or EnrollmentId(new_id("enr")),
        campaign_id=CAMPAIGN,
        campaign_version=1,
        account_id=account_id,
        contact_point_id=CONTACT,
        sending_identity_id=SENDER,
        state=models.EnrollmentState.ENROLLED,
        current_step=0,
        next_send_at=NOW,
        enrolled_at=NOW,
        stopped_at=None,
        stop_reason=None,
        idempotency_key=IdempotencyKey(key),
    )


def _suppression(*, key: str = "suppression-key-1") -> object:
    models = importlib.import_module("domains.outreach.models")
    schemas = importlib.import_module("domains.outreach.schemas")
    return models.SuppressionEntry(
        tenant_id=TENANT_A,
        suppression_id=SuppressionId(new_id("sup")),
        target=schemas.SuppressionTarget(account_id=ACCOUNT),
        reason=models.SuppressionReason.MANUAL_BLOCK,
        occurred_at=NOW,
        source_ref="manual_record_1",
        idempotency_key=IdempotencyKey(key),
        created_at=NOW,
    )


def _attempt(*, key: str = "attempt-key-1") -> object:
    models = importlib.import_module("domains.outreach.models")
    return models.MessageAttempt(
        tenant_id=TENANT_A,
        attempt_id=MessageAttemptId(new_id("mat")),
        message_id=MessageId(new_id("msg")),
        campaign_id=CAMPAIGN,
        enrollment_id=EnrollmentId(new_id("enr")),
        campaign_version=1,
        step_number=1,
        sending_identity_id=SENDER,
        idempotency_key=IdempotencyKey(key),
        state=models.MessageAttemptState.RESERVED,
        provider_ref=None,
        failure_category=None,
        created_at=NOW,
        updated_at=NOW,
    )


async def _seed_campaign(factory: async_sessionmaker[AsyncSession]) -> None:
    uow_type = _load("infra.db.outreach_uow", "SqlAlchemyOutreachUnitOfWork")
    campaign, version = _campaign_graph()
    async with uow_type(factory, TENANT_A, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)


async def test_campaign_repository_is_tenant_bound_and_roundtrips_versions(
    outreach_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """跨租户读空、写 typed violation+CRITICAL；版本与步骤完整回读。"""
    repository_type = _load("infra.db.repositories.outreach", "CampaignRepositoryImpl")
    campaign, version = _campaign_graph()
    async with outreach_factory() as session:
        repo = repository_type(session, TENANT_A)
        await repo.add(campaign, version)
        await session.commit()
    async with outreach_factory() as session:
        assert await repository_type(session, TENANT_B).get(TENANT_B, CAMPAIGN) is None
        repo = repository_type(session, TENANT_A)
        found = await repo.get(TENANT_A, CAMPAIGN)
        assert found is not None and found.current_version == 1
        assert await repo.get_for_update(TENANT_A, CAMPAIGN) == found
        found_version = await repo.get_version(TENANT_A, CAMPAIGN, 1)
        assert found_version == version
        scope = importlib.import_module("domains.outreach.permissions")
        assert await repo.list_scoped(
            TENANT_A, scope.OutreachScope(level=scope.ScopeLevel.TENANT), 10
        ) == [found]
    with caplog.at_level(logging.CRITICAL, logger="security.tenant_isolation"):
        async with outreach_factory() as session:
            with pytest.raises(TenantIsolationViolation):
                await repository_type(session, TENANT_B).add(campaign, version)
    records = [r for r in caplog.records if r.name == "security.tenant_isolation"]
    assert [(r.levelname, r.message) for r in records] == [
        ("CRITICAL", "检测到跨租户数据隔离违规")
    ]


async def test_enrollment_atomic_outcomes_locking_and_order(
    outreach_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同 key 同 payload幂等、异 payload冲突、活动企业冲突与 ASC scoped list。"""
    await _seed_campaign(outreach_factory)
    repository_type = _load(
        "infra.db.repositories.outreach", "EnrollmentRepositoryImpl"
    )
    repository = importlib.import_module("domains.outreach.repository")
    first = _enrollment(enrollment_id=EnrollmentId(new_id("enr")))
    async with outreach_factory() as session:
        repo = repository_type(session, TENANT_A)
        created = await repo.insert_if_absent(first)
        same = await repo.insert_if_absent(first)
        conflict = await repo.insert_if_absent(
            _enrollment(
                enrollment_id=EnrollmentId(new_id("enr")),
                account_id=ProspectAccountId(new_id("acc")),
                key="enrollment-key-1",
            )
        )
        account_conflict = await repo.insert_if_absent(
            _enrollment(
                enrollment_id=EnrollmentId(new_id("enr")),
                key="enrollment-key-2",
            )
        )
        assert created.status is repository.EnrollmentInsertStatus.CREATED
        assert same.status is repository.EnrollmentInsertStatus.EXISTING
        assert conflict.status is repository.EnrollmentInsertStatus.IDEMPOTENCY_CONFLICT
        assert (
            account_conflict.status
            is repository.EnrollmentInsertStatus.ACCOUNT_CONFLICT
        )
        locked = await repo.get_for_update(TENANT_A, first.enrollment_id)
        assert locked == first
        second = _enrollment(
            enrollment_id=EnrollmentId(new_id("enr")),
            account_id=ProspectAccountId(new_id("acc")),
            key="enrollment-key-3",
        )
        assert (
            await repo.insert_if_absent(second)
        ).status is repository.EnrollmentInsertStatus.CREATED
        schemas = importlib.import_module("domains.outreach.schemas")
        assert await repo.lock_matching_active(
            TENANT_A, schemas.SuppressionTarget(account_id=first.account_id)
        ) == [first]
        permissions = importlib.import_module("domains.outreach.permissions")
        listed = await repo.list_scoped(
            TENANT_A,
            permissions.OutreachScope(level=permissions.ScopeLevel.TENANT),
            10,
        )
        assert [value.enrollment_id for value in listed] == sorted(
            [first.enrollment_id, second.enrollment_id]
        )
        await session.commit()


async def test_suppression_attempt_and_quota_atomic_outcomes(
    outreach_factory: async_sessionmaker[AsyncSession],
) -> None:
    """append/create/cap 均不依赖数据库错误字符串。"""
    await _seed_campaign(outreach_factory)
    repositories = importlib.import_module("infra.db.repositories.outreach")
    contract = importlib.import_module("domains.outreach.repository")
    suppression = _suppression()
    enrollment = _enrollment(enrollment_id=EnrollmentId(new_id("enr")))
    attempt = _attempt()
    attempt.enrollment_id = enrollment.enrollment_id
    async with outreach_factory() as session:
        enrollment_repo = repositories.EnrollmentRepositoryImpl(session, TENANT_A)
        await enrollment_repo.insert_if_absent(enrollment)
        suppression_repo = repositories.SuppressionRepositoryImpl(session, TENANT_A)
        assert (
            await suppression_repo.append_if_absent(suppression)
        ).status is contract.AppendStatus.CREATED
        assert (
            await suppression_repo.append_if_absent(suppression)
        ).status is contract.AppendStatus.EXISTING
        conflicting = _suppression(key="suppression-key-1")
        assert (
            await suppression_repo.append_if_absent(conflicting)
        ).status is contract.AppendStatus.CONFLICT
        assert (
            await suppression_repo.find_current(TENANT_A, suppression.target)
            == suppression
        )
        permissions = importlib.import_module("domains.outreach.permissions")
        assert await suppression_repo.list_scoped(
            TENANT_A,
            permissions.OutreachScope(level=permissions.ScopeLevel.TENANT),
            10,
        ) == [suppression]

        attempt_repo = repositories.MessageAttemptRepositoryImpl(session, TENANT_A)
        assert (
            await attempt_repo.create_if_absent(attempt)
        ).status is contract.AppendStatus.CREATED
        assert (
            await attempt_repo.create_if_absent(attempt)
        ).status is contract.AppendStatus.EXISTING
        conflicting_attempt = _attempt(key="attempt-key-1")
        conflicting_attempt.enrollment_id = enrollment.enrollment_id
        assert (
            await attempt_repo.create_if_absent(conflicting_attempt)
        ).status is contract.AppendStatus.CONFLICT
        assert (
            await attempt_repo.get_for_update(TENANT_A, attempt.attempt_id) == attempt
        )
        assert (
            await attempt_repo.get_by_key(TENANT_A, attempt.idempotency_key) == attempt
        )

        quotas = repositories.QuotaRepositoryImpl(session, TENANT_A)
        first = await quotas.reserve_new_contact(TENANT_A, CAMPAIGN, NOW.date(), 1)
        capped = await quotas.reserve_new_contact(TENANT_A, CAMPAIGN, NOW.date(), 1)
        message = await quotas.reserve_message(TENANT_A, CAMPAIGN, NOW.date(), 2)
        assert first.status is contract.QuotaReservationStatus.RESERVED
        assert capped.status is contract.QuotaReservationStatus.CAP_REACHED
        assert message.status is contract.QuotaReservationStatus.RESERVED
        assert await quotas.get_usage(TENANT_A, CAMPAIGN, NOW.date()) == message.winner
        await session.commit()


async def test_action_repository_is_idempotent_and_append_only(
    outreach_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_campaign(outreach_factory)
    models = importlib.import_module("domains.outreach.models")
    repository_type = _load("infra.db.repositories.outreach", "ActionRepositoryImpl")
    action = models.ActionRecord(
        tenant_id=TENANT_A,
        action_id=new_id("act"),
        action_key="campaign:create:v1",
        action="campaign:create",
        entity_id=str(CAMPAIGN),
        actor_id=str(EmployeeId(new_id("emp"))),
        occurred_at=NOW,
    )
    async with outreach_factory() as session:
        repo = repository_type(session, TENANT_A)
        assert await repo.append(action)
        assert not await repo.append(action)
        await session.commit()


async def test_uow_commit_failure_rolls_back_campaign_action_and_outbox(
    outreach_engine: AsyncEngine,
) -> None:
    """业务、审计和 outbox 必须共享事务；commit failure 不留半套事实。"""
    uow_type = _load("infra.db.outreach_uow", "SqlAlchemyOutreachUnitOfWork")
    rows = importlib.import_module("infra.db.tables")
    models = importlib.import_module("domains.outreach.models")
    events = importlib.import_module("shared.events.catalog")
    campaign, version = _campaign_graph()

    class CommitFailure(RuntimeError):
        pass

    class CommitFailingSession(AsyncSession):
        async def commit(self) -> None:
            raise CommitFailure()

    failing_factory = async_sessionmaker(
        outreach_engine,
        expire_on_commit=False,
        class_=CommitFailingSession,
    )
    reader_factory = async_sessionmaker(outreach_engine, expire_on_commit=False)
    with pytest.raises(CommitFailure):
        async with uow_type(failing_factory, TENANT_A, now=lambda: NOW) as uow:
            await uow.campaigns.add(campaign, version)
            await uow.actions.append(
                models.ActionRecord(
                    tenant_id=TENANT_A,
                    action_id=new_id("act"),
                    action_key="campaign:create:commit-failure",
                    action="campaign:create",
                    entity_id=str(CAMPAIGN),
                    actor_id=str(EmployeeId(new_id("emp"))),
                    occurred_at=NOW,
                )
            )
            await uow.bus.publish(
                events.MessageSent(
                    tenant_id=TENANT_A,
                    occurred_at=NOW,
                    run_id=None,
                    message_id=MessageId(new_id("msg")),
                    campaign_id=CAMPAIGN,
                    sending_identity_id=SENDER,
                )
            )
    async with reader_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.OutreachCampaignRow)
                .where(rows.OutreachCampaignRow.tenant_id == TENANT_A)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.OutreachActionRow)
                .where(rows.OutreachActionRow.tenant_id == TENANT_A)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.OutboxEventRow)
                .where(rows.OutboxEventRow.tenant_id == TENANT_A)
            )
            == 0
        )


async def test_uow_cleanup_preserves_primary_base_exception() -> None:
    """rollback/close 的 BaseException 不得覆盖 body primary。"""
    uow_type = _load("infra.db.outreach_uow", "SqlAlchemyOutreachUnitOfWork")

    class CleanupFailure(BaseException):
        pass

    class PrimaryFailure(BaseException):
        pass

    class Session:
        async def rollback(self) -> None:
            raise CleanupFailure()

        async def close(self) -> None:
            raise CleanupFailure()

    class Factory:
        def __call__(self) -> Session:
            return Session()

    primary = PrimaryFailure()
    with pytest.raises(PrimaryFailure) as raised:
        async with uow_type(Factory(), TENANT_A):
            raise primary
    assert raised.value is primary


async def test_outreach_tables_can_be_read_with_explicit_tenant_filters(
    outreach_factory: async_sessionmaker[AsyncSession],
) -> None:
    """每张新表都保留 tenant_id，测试读回也不使用跨租户捷径。"""
    names = (
        "outreach_campaigns",
        "outreach_campaign_versions",
        "outreach_sequence_steps",
        "outreach_enrollments",
        "outreach_suppressions",
        "outreach_daily_quotas",
        "outreach_message_attempts",
        "outreach_actions",
    )
    async with outreach_factory() as session:
        for name in names:
            assert (
                await session.scalar(
                    text(f"SELECT count(*) FROM {name} WHERE tenant_id=:tenant"),
                    {"tenant": str(TENANT_A)},
                )
                >= 0
            )
