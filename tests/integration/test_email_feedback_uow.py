"""整页邮件反馈外层 UoW 的单 session、审计与清理语义。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.errors import PermissionDenied, TenantIsolationViolation
from shared.schemas.identifiers import TenantId, new_id

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


def _load(module: str, symbol: str) -> object:
    try:
        return getattr(importlib.import_module(module), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"缺少邮件反馈 UoW {module}.{symbol}: {exc}")


@pytest_asyncio.fixture
async def uow_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = _load("infra.db.session", "create_engine_from")(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


class AuditSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[dict[str, str]] = []

    def log(self, **record: str) -> None:
        if self.fail:
            raise RuntimeError("audit sink secret marker")
        self.records.append(dict(record))


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class Participant:
    def __init__(self, factory: object, audit: object) -> None:
        self.factory = factory
        self.audit = audit

    async def write_action(self, tenant_id: TenantId, key: str) -> None:
        models = importlib.import_module("domains.outreach.models")
        events = importlib.import_module("shared.events.catalog")
        async with self.factory(tenant_id) as bound:
            await bound.actions.append(
                models.ActionRecord(
                    tenant_id=tenant_id,
                    action_id=new_id("act"),
                    action_key=key,
                    action="hard_bounce:apply",
                    entity_id=new_id("sup"),
                    actor_id="system:feedback",
                    occurred_at=NOW,
                )
            )
            await bound.bus.publish(
                events.SuppressionAdded(
                    tenant_id=tenant_id,
                    occurred_at=NOW,
                    run_id=None,
                    scope="contact",
                    target_id=new_id("cp"),
                    reason="hard_bounce",
                )
            )
        self.audit.log(
            actor="system:feedback",
            action="hard_bounce:apply",
            tenant_id=tenant_id,
            scope="system",
            rule="phase1:system:system:hard_bounce:apply",
        )


def _builder(factory: object, audit: object) -> Participant:
    return Participant(factory, audit)


def _real_outreach_builder(
    tenant: TenantId, now: object = lambda: NOW
) -> object:
    service_impl = importlib.import_module("domains.outreach.service_impl")
    permissions = importlib.import_module("domains.outreach.permissions")

    def build(factory: object, audit: object) -> object:
        unused = object()
        return service_impl.OutreachServiceImpl(
            factory,
            unused,
            unused,
            unused,
            unused,
            permissions.Phase1OutreachAuthorizer(tenant),
            audit,
            now=now,
        )

    return build


def _real_sending_identity_builder(
    tenant: TenantId, now: object = lambda: NOW
) -> object:
    service_impl = importlib.import_module("domains.sending_identity.service_impl")
    permissions = importlib.import_module("domains.sending_identity.permissions")

    def build(factory: object, audit: object) -> object:
        return service_impl.SendingIdentityServiceImpl(
            factory,
            permissions.Phase1SendingIdentityAuthorizer(tenant),
            audit,
            now=now,
        )

    return build


async def _exercise_real_sending_identity(
    uow: object,
    tenant: TenantId,
    clock: Clock,
) -> None:
    si_permissions = importlib.import_module("domains.sending_identity.permissions")
    si_schemas = importlib.import_module("domains.sending_identity.schemas")
    si_models = importlib.import_module("domains.sending_identity.models")

    boss = si_permissions.Actor(
        "boss:feedback",
        si_permissions.SendingIdentityScope(
            level=si_permissions.ScopeLevel.TENANT
        ),
        "boss",
    )
    identity_id = await uow.sending_identities.register(
        tenant,
        si_schemas.IdentityRegisterRequest(
            address="feedback@feedback-atomic.example",
            domain="feedback-atomic.example",
            role=si_models.DomainRole.COLD_OUTREACH,
            connector_ref="feedback_connector_ref",
        ),
        actor=boss,
    )
    system = si_permissions.Actor(
        "system:feedback",
        si_permissions.SendingIdentityScope(
            level=si_permissions.ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )
    await uow.sending_identities.begin_authentication(
        tenant, identity_id, actor=boss
    )
    await uow.sending_identities.record_authentication_result(
        tenant,
        identity_id,
        si_schemas.AuthenticationResult(
            checked_at=clock.now(),
            spf_passed=True,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref="feedback_auth_ref",
        ),
        actor=system,
    )
    await uow.sending_identities.start_warmup(
        tenant, identity_id, 5, actor=boss
    )
    clock.value += timedelta(days=28)
    await uow.sending_identities.advance_warmup(
        tenant, identity_id, actor=system
    )


async def _exercise_real_outreach(
    uow: object,
    tenant: TenantId,
    clock: Clock,
) -> None:
    outreach_permissions = importlib.import_module("domains.outreach.permissions")
    outreach_schemas = importlib.import_module("domains.outreach.schemas")
    outreach_models = importlib.import_module("domains.outreach.models")
    target = outreach_schemas.SuppressionTarget(
        contact_point_id=new_id("cp")
    )
    outreach_actor = outreach_permissions.Actor(
        "system:feedback",
        outreach_permissions.OutreachScope(
            level=outreach_permissions.ScopeLevel.SYSTEM,
            allowed_suppression_targets=frozenset({target.canonical_id}),
        ),
        "system",
    )
    result = await uow.outreach.add_suppression(
        tenant,
        outreach_schemas.SuppressionRequest(
            target,
            outreach_models.SuppressionReason.HARD_BOUNCE,
            clock.now(),
            "feedback_event_ref",
            f"feedback-suppression-{new_id('sup')}",
        ),
        actor=outreach_actor,
    )
    assert result.created is True


async def _exercise_real_domain_services(
    uow: object,
    tenant: TenantId,
    clock: Clock,
) -> None:
    await _exercise_real_sending_identity(uow, tenant, clock)
    await _exercise_real_outreach(uow, tenant, clock)


async def _stage_feedback_page(uow: object, tenant: TenantId, event_id: str) -> None:
    contract = importlib.import_module("workflows.email_feedback.repository")
    shared = importlib.import_module("shared.schemas.email_feedback")
    cursor = await uow.cursors.lock_expected(
        tenant, "feedback-primary", None
    )
    result = await uow.receipts.append_if_absent(
        contract.FeedbackReceipt(
            tenant_id=tenant,
            mailbox_alias="feedback-primary",
            provider_event_id=event_id,
            ordinal=0,
            kind=shared.EmailFeedbackKind.UNPARSEABLE,
            occurred_at=NOW,
            result=shared.EmailFeedbackResult.QUARANTINED,
            attempt_id=None,
            enrollment_id=None,
            account_id=None,
            contact_point_id=None,
            sending_identity_id=None,
            created_at=NOW,
        )
    )
    assert result.status is contract.FeedbackReceiptAppendStatus.CREATED
    assert await uow.quarantines.append_if_absent(
        contract.FeedbackQuarantine(
            tenant_id=tenant,
            mailbox_alias="feedback-primary",
            provider_event_id=event_id,
            reason=shared.EmailFeedbackQuarantineReason.MALFORMED,
            provider_ref_digest="d" * 64,
            created_at=NOW,
        )
    )
    await uow.cursors.advance(cursor, "opaque-next-cursor", NOW)


@pytest.mark.asyncio
async def test_outer_uow_owns_one_session_and_flushes_audit_after_commit(
    uow_engine: AsyncEngine,
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    rows = importlib.import_module("infra.db.tables")
    tenant = TenantId(new_id("tn"))
    sink = AuditSink()
    trace: list[str] = []

    class TracedSession(AsyncSession):
        async def commit(self) -> None:
            trace.append("commit")
            await super().commit()

    factory = async_sessionmaker(
        uow_engine, expire_on_commit=False, class_=TracedSession
    )
    async with uow_type(
        factory,
        tenant,
        outreach_builder=_builder,
        sending_identity_builder=_builder,
        audit_sink=sink,
        now=lambda: NOW,
    ) as uow:
        assert uow.cursors._session is uow.receipts._session
        assert uow.receipts._session is uow.quarantines._session
        assert uow.quarantines._session is uow.tokens._session
        await _stage_feedback_page(uow, tenant, "a" * 64)
        await uow.outreach.write_action(tenant, "feedback:uow:success")
        assert sink.records == []
    trace.extend("audit" for _ in sink.records)
    assert trace == ["commit", "audit"]
    assert len(sink.records) == 1
    async with factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachActionRow)
            .where(
                rows.OutreachActionRow.tenant_id == tenant,
                rows.OutreachActionRow.action_key == "feedback:uow:success",
            )
        ) == 1
        assert await session.scalar(
            select(func.count())
            .select_from(rows.EmailFeedbackReceiptRow)
            .where(rows.EmailFeedbackReceiptRow.tenant_id == tenant)
        ) == 1
        assert await session.scalar(
            select(func.count())
            .select_from(rows.EmailFeedbackQuarantineRow)
            .where(rows.EmailFeedbackQuarantineRow.tenant_id == tenant)
        ) == 1
        cursor = await session.get(
            rows.EmailFeedbackCursorRow, (str(tenant), "feedback-primary")
        )
        assert cursor is not None
        assert (cursor.provider_cursor, cursor.version) == ("opaque-next-cursor", 1)
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(rows.OutboxEventRow.tenant_id == tenant)
        ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("participant_name", ("outreach", "sending_identities"))
async def test_real_domain_authorization_deny_is_not_discarded_by_outer_rollback(
    uow_engine: AsyncEngine,
    participant_name: str,
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    tenant = TenantId(new_id("tn"))
    sink = AuditSink()
    factory = async_sessionmaker(uow_engine, expire_on_commit=False)

    with pytest.raises(PermissionDenied):
        async with uow_type(
            factory,
            tenant,
            outreach_builder=_real_outreach_builder(tenant),
            sending_identity_builder=_real_sending_identity_builder(tenant),
            audit_sink=sink,
            now=lambda: NOW,
        ) as uow:
            if participant_name == "outreach":
                await uow.outreach.add_suppression(tenant, object(), actor=None)
            else:
                await uow.sending_identities.get(
                    tenant,
                    new_id("sid"),
                    actor=None,
                )

    expected_action = (
        "suppression:add"
        if participant_name == "outreach"
        else "identity:read"
    )
    assert sink.records == [
        {
            "actor": "unknown",
            "action": expected_action,
            "tenant_id": tenant,
            "scope": "none",
            "rule": "deny:authorization",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", (False, True))
async def test_real_domain_services_share_feedback_page_transaction(
    uow_engine: AsyncEngine,
    commit_fails: bool,
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    rows = importlib.import_module("infra.db.tables")
    tenant = TenantId(new_id("tn"))
    sink = AuditSink()
    clock = Clock(NOW - timedelta(days=28))

    class CommitFailure(RuntimeError):
        pass

    sentinel = CommitFailure("real services commit failure marker")

    class ControlledSession(AsyncSession):
        async def commit(self) -> None:
            if commit_fails:
                raise sentinel
            await super().commit()

    factory = async_sessionmaker(
        uow_engine, expire_on_commit=False, class_=ControlledSession
    )

    async def exercise() -> None:
        async with uow_type(
            factory,
            tenant,
            outreach_builder=_real_outreach_builder(tenant, clock.now),
            sending_identity_builder=_real_sending_identity_builder(
                tenant, clock.now
            ),
            audit_sink=sink,
            now=clock.now,
        ) as uow:
            await _exercise_real_domain_services(uow, tenant, clock)
            await _stage_feedback_page(uow, tenant, "f" * 64)
            assert sink.records == []

    if commit_fails:
        with pytest.raises(CommitFailure) as raised:
            await exercise()
        assert raised.value is sentinel
    else:
        await exercise()

    expected = 0 if commit_fails else 1
    reader = async_sessionmaker(uow_engine, expire_on_commit=False)
    async with reader() as session:
        for row_type in (
            rows.SendingDomainRow,
            rows.SendingIdentityRow,
            rows.AuthenticationCheckRow,
            rows.OutreachSuppressionRow,
            rows.EmailFeedbackReceiptRow,
            rows.EmailFeedbackQuarantineRow,
            rows.EmailFeedbackCursorRow,
        ):
            assert await session.scalar(
                select(func.count())
                .select_from(row_type)
                .where(row_type.tenant_id == tenant)
            ) == expected
        assert await session.scalar(
            select(func.count())
            .select_from(rows.IdentityActionRow)
            .where(rows.IdentityActionRow.tenant_id == tenant)
        ) == (4 if not commit_fails else 0)
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachActionRow)
            .where(rows.OutreachActionRow.tenant_id == tenant)
        ) == expected
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(rows.OutboxEventRow.tenant_id == tenant)
        ) == (2 if not commit_fails else 0)
    assert len(sink.records) == (6 if not commit_fails else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_boundary",
    ("outreach_repository", "sending_identity_repository", "outbox"),
)
async def test_real_domain_boundary_failure_rolls_back_entire_feedback_page(
    uow_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    rows = importlib.import_module("infra.db.tables")
    outreach_repositories = importlib.import_module(
        "infra.db.repositories.outreach"
    )
    sending_repositories = importlib.import_module(
        "infra.db.repositories.sending_identities"
    )
    outbox = importlib.import_module("infra.db.outbox")
    tenant = TenantId(new_id("tn"))
    sink = AuditSink()
    clock = Clock(NOW - timedelta(days=28))
    sentinel = RuntimeError(f"{failure_boundary} failure marker")

    async def fail_repository(*_args: object, **_kwargs: object) -> None:
        raise sentinel

    if failure_boundary == "outreach_repository":
        monkeypatch.setattr(
            outreach_repositories.SuppressionRepositoryImpl,
            "append_if_absent",
            fail_repository,
        )
    elif failure_boundary == "sending_identity_repository":
        monkeypatch.setattr(
            sending_repositories.SendingIdentityRepositoryImpl,
            "register_if_address_absent",
            fail_repository,
        )
    else:
        original_publish = outbox.PostgresEventBus.publish
        publish_calls = 0

        async def fail_second_publish(self: object, event: object) -> None:
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 2:
                raise sentinel
            await original_publish(self, event)

        monkeypatch.setattr(
            outbox.PostgresEventBus,
            "publish",
            fail_second_publish,
        )

    factory = async_sessionmaker(uow_engine, expire_on_commit=False)
    with pytest.raises(RuntimeError) as raised:
        async with uow_type(
            factory,
            tenant,
            outreach_builder=_real_outreach_builder(tenant, clock.now),
            sending_identity_builder=_real_sending_identity_builder(
                tenant, clock.now
            ),
            audit_sink=sink,
            now=clock.now,
        ) as uow:
            await _stage_feedback_page(uow, tenant, "e" * 64)
            if failure_boundary == "sending_identity_repository":
                await _exercise_real_outreach(uow, tenant, clock)
                await _exercise_real_sending_identity(uow, tenant, clock)
            else:
                await _exercise_real_sending_identity(uow, tenant, clock)
                await _exercise_real_outreach(uow, tenant, clock)
    assert raised.value is sentinel
    assert sink.records == []

    reader = async_sessionmaker(uow_engine, expire_on_commit=False)
    async with reader() as session:
        for row_type in (
            rows.SendingDomainRow,
            rows.SendingIdentityRow,
            rows.AuthenticationCheckRow,
            rows.IdentityActionRow,
            rows.OutreachSuppressionRow,
            rows.OutreachActionRow,
            rows.OutboxEventRow,
            rows.EmailFeedbackCursorRow,
            rows.EmailFeedbackReceiptRow,
            rows.EmailFeedbackQuarantineRow,
        ):
            assert await session.scalar(
                select(func.count())
                .select_from(row_type)
                .where(row_type.tenant_id == tenant)
            ) == 0


@pytest.mark.asyncio
async def test_outer_commit_failure_rolls_back_domain_outbox_and_discards_audit(
    uow_engine: AsyncEngine,
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    rows = importlib.import_module("infra.db.tables")
    tenant = TenantId(new_id("tn"))
    sink = AuditSink()

    class CommitFailure(RuntimeError):
        pass

    sentinel = CommitFailure("commit failure marker")

    class CommitFailingSession(AsyncSession):
        async def commit(self) -> None:
            raise sentinel

    factory = async_sessionmaker(
        uow_engine, expire_on_commit=False, class_=CommitFailingSession
    )
    with pytest.raises(CommitFailure) as captured:
        async with uow_type(
            factory,
            tenant,
            outreach_builder=_builder,
            sending_identity_builder=_builder,
            audit_sink=sink,
            now=lambda: NOW,
        ) as uow:
            await _stage_feedback_page(uow, tenant, "b" * 64)
            await uow.outreach.write_action(tenant, "feedback:uow:rollback")
    assert captured.value is sentinel
    assert sink.records == []
    reader = async_sessionmaker(uow_engine, expire_on_commit=False)
    async with reader() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachActionRow)
            .where(
                rows.OutreachActionRow.tenant_id == tenant,
                rows.OutreachActionRow.action_key == "feedback:uow:rollback",
            )
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.EmailFeedbackReceiptRow)
            .where(rows.EmailFeedbackReceiptRow.tenant_id == tenant)
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.EmailFeedbackQuarantineRow)
            .where(rows.EmailFeedbackQuarantineRow.tenant_id == tenant)
        ) == 0
        assert await session.get(
            rows.EmailFeedbackCursorRow, (str(tenant), "feedback-primary")
        ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("participant_name", "fail_after_outbox"),
    (("outreach", False), ("sending_identities", False), ("outreach", True)),
)
async def test_body_failures_roll_back_feedback_domains_and_outbox(
    uow_engine: AsyncEngine,
    participant_name: str,
    fail_after_outbox: bool,
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    rows = importlib.import_module("infra.db.tables")
    tenant = TenantId(new_id("tn"))
    sink = AuditSink()
    sentinel = RuntimeError("private repository failure marker")

    class FailingParticipant(Participant):
        async def write_action(self, tenant_id: TenantId, key: str) -> None:
            if fail_after_outbox:
                await super().write_action(tenant_id, key)
            raise sentinel

    def failing_builder(factory: object, audit: object) -> FailingParticipant:
        return FailingParticipant(factory, audit)

    factory = async_sessionmaker(uow_engine, expire_on_commit=False)
    with pytest.raises(RuntimeError) as raised:
        async with uow_type(
            factory,
            tenant,
            outreach_builder=failing_builder,
            sending_identity_builder=failing_builder,
            audit_sink=sink,
            now=lambda: NOW,
        ) as uow:
            await _stage_feedback_page(uow, tenant, "c" * 64)
            await getattr(uow, participant_name).write_action(
                tenant, f"feedback:uow:{participant_name}:{fail_after_outbox}"
            )
    assert raised.value is sentinel
    assert sink.records == []
    async with factory() as session:
        for row_type in (
            rows.EmailFeedbackCursorRow,
            rows.EmailFeedbackReceiptRow,
            rows.EmailFeedbackQuarantineRow,
            rows.OutreachActionRow,
            rows.OutboxEventRow,
        ):
            assert await session.scalar(
                select(func.count())
                .select_from(row_type)
                .where(row_type.tenant_id == tenant)
            ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(rows.OutboxEventRow.tenant_id == tenant)
        ) == 0


@pytest.mark.asyncio
async def test_post_commit_audit_failure_is_fixed_logged_not_retry_signal(
    uow_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    tenant = TenantId(new_id("tn"))
    sink = AuditSink(fail=True)
    factory = async_sessionmaker(uow_engine, expire_on_commit=False)
    with caplog.at_level("ERROR", logger="infra.db.email_feedback.uow"):
        async with uow_type(
            factory,
            tenant,
            outreach_builder=_builder,
            sending_identity_builder=_builder,
            audit_sink=sink,
            now=lambda: NOW,
        ) as uow:
            await uow.outreach.write_action(tenant, "feedback:uow:audit-failure")
    records = [record for record in caplog.records if record.name == "infra.db.email_feedback.uow"]
    assert [(record.levelname, record.message) for record in records] == [
        ("ERROR", "邮件反馈提交后审计刷新失败")
    ]
    assert "secret marker" not in caplog.text


@pytest.mark.asyncio
async def test_post_commit_audit_cancellation_cannot_replay_committed_page(
    uow_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    tenant = TenantId(new_id("tn"))

    class CancelledAuditSink(AuditSink):
        def log(self, **record: str) -> None:
            raise asyncio.CancelledError("audit cancellation private marker")

    factory = async_sessionmaker(uow_engine, expire_on_commit=False)
    with caplog.at_level("ERROR", logger="infra.db.email_feedback.uow"):
        async with uow_type(
            factory,
            tenant,
            outreach_builder=_builder,
            sending_identity_builder=_builder,
            audit_sink=CancelledAuditSink(),
            now=lambda: NOW,
        ) as uow:
            await uow.outreach.write_action(tenant, "feedback:uow:audit-cancel")
    records = [
        record for record in caplog.records
        if record.name == "infra.db.email_feedback.uow"
    ]
    assert [(record.levelname, record.message) for record in records] == [
        ("ERROR", "邮件反馈提交后审计刷新失败")
    ]
    assert "private marker" not in caplog.text


@pytest.mark.asyncio
async def test_outer_uow_cleanup_never_replaces_primary_base_exception() -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    tenant = TenantId(new_id("tn"))

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
        async with uow_type(
            Factory(),
            tenant,
            outreach_builder=_builder,
            sending_identity_builder=_builder,
            audit_sink=AuditSink(),
            now=lambda: NOW,
        ):
            raise primary
    assert raised.value is primary


@pytest.mark.asyncio
async def test_bound_domain_factory_rejects_other_tenant_before_repository_access(
    uow_engine: AsyncEngine,
) -> None:
    uow_type = _load("infra.db.email_feedback_uow", "SqlAlchemyFeedbackPageUnitOfWork")
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    factory = async_sessionmaker(uow_engine, expire_on_commit=False)
    async with uow_type(
        factory,
        tenant,
        outreach_builder=_builder,
        sending_identity_builder=_builder,
        audit_sink=AuditSink(),
        now=lambda: NOW,
    ) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.outreach.write_action(other, "feedback:uow:wrong-tenant")
