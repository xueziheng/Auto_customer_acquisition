"""邮件反馈 PostgreSQL 仓储的租户、CAS、幂等与安全持久化契约。"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.errors import TenantIsolationViolation, TransientError, ValidationError
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
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


def _load(module: str, symbol: str) -> object:
    try:
        return getattr(importlib.import_module(module), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"缺少邮件反馈持久化 {module}.{symbol}: {exc}")


@pytest_asyncio.fixture
async def feedback_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    create_engine_from = _load("infra.db.session", "create_engine_from")
    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def feedback_factory(
    feedback_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(feedback_engine, expire_on_commit=False)


def _ids() -> dict[str, object]:
    return {
        "tenant": TenantId(new_id("tn")),
        "other_tenant": TenantId(new_id("tn")),
        "campaign": CampaignId(new_id("cmp")),
        "attempt": MessageAttemptId(new_id("mat")),
        "enrollment": EnrollmentId(new_id("enr")),
        "account": ProspectAccountId(new_id("acc")),
        "contact": ContactPointId(new_id("cp")),
        "identity": SendingIdentityId(new_id("sid")),
    }


async def _seed_feedback_prerequisites(
    factory: async_sessionmaker[AsyncSession], ids: dict[str, object]
) -> None:
    """用公开触达仓储种下 receipt/token 的复合外键前置。"""
    models = importlib.import_module("domains.outreach.models")
    uow_type = _load("infra.db.outreach_uow", "SqlAlchemyOutreachUnitOfWork")
    actor = EmployeeId(new_id("emp"))
    boundary = models.CampaignBoundary(
        markets=["US"],
        target_entity_types=["importer"],
        allowed_categories=["hardware"],
        sender_identity_ids=[ids["identity"]],
        steps=[models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0)],
        daily_new_contact_limit=2,
        daily_total_message_limit=3,
        handoff_triggers=[],
        stop_on_reply=True,
    )
    campaign = models.Campaign(
        tenant_id=ids["tenant"],
        campaign_id=ids["campaign"],
        state=models.CampaignState.DRAFT,
        current_version=1,
        created_by=actor,
        created_at=NOW,
    )
    version = models.CampaignVersion(
        tenant_id=ids["tenant"],
        campaign_id=ids["campaign"],
        version=1,
        name="Feedback repository prerequisites",
        boundary=boundary,
        created_by=actor,
        created_at=NOW,
    )
    enrollment = models.Enrollment(
        tenant_id=ids["tenant"],
        enrollment_id=ids["enrollment"],
        campaign_id=ids["campaign"],
        campaign_version=1,
        account_id=ids["account"],
        contact_point_id=ids["contact"],
        sending_identity_id=ids["identity"],
        state=models.EnrollmentState.ENROLLED,
        current_step=0,
        next_send_at=NOW,
        enrolled_at=NOW,
        stopped_at=None,
        stop_reason=None,
        idempotency_key=IdempotencyKey(f"feedback-enrollment-{new_id('enr')}"),
    )
    attempt = models.MessageAttempt(
        tenant_id=ids["tenant"],
        attempt_id=ids["attempt"],
        message_id=MessageId(new_id("msg")),
        campaign_id=ids["campaign"],
        enrollment_id=ids["enrollment"],
        campaign_version=1,
        step_number=1,
        sending_identity_id=ids["identity"],
        idempotency_key=IdempotencyKey(f"feedback-attempt-{new_id('mat')}"),
        state=models.MessageAttemptState.RESERVED,
        provider_ref=None,
        failure_category=None,
        created_at=NOW,
        updated_at=NOW,
    )
    async with uow_type(factory, ids["tenant"], now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)
        await uow.enrollments.insert_if_absent(enrollment)
        await uow.attempts.create_if_absent(attempt)

    repositories = importlib.import_module("infra.db.repositories.email_feedback")
    async with factory() as session:
        await repositories.FeedbackCursorRepositoryImpl(
            session, ids["tenant"], now=lambda: NOW
        ).lock_expected(ids["tenant"], "feedback-primary", None)
        await session.commit()


def _receipt(ids: dict[str, object], **changes: object) -> object:
    contract = importlib.import_module("workflows.email_feedback.repository")
    shared = importlib.import_module("shared.schemas.email_feedback")
    values: dict[str, object] = {
        "tenant_id": ids["tenant"],
        "mailbox_alias": "feedback-primary",
        "provider_event_id": "a" * 64,
        "item_fingerprint": "f" * 64,
        "ordinal": 0,
        "kind": shared.EmailFeedbackKind.HARD_BOUNCE,
        "occurred_at": NOW,
        "result": shared.EmailFeedbackResult.APPLIED,
        "attempt_id": ids["attempt"],
        "enrollment_id": ids["enrollment"],
        "account_id": ids["account"],
        "contact_point_id": ids["contact"],
        "sending_identity_id": ids["identity"],
        "created_at": NOW,
    }
    values.update(changes)
    return contract.FeedbackReceipt(**values)


def _token(ids: dict[str, object], **changes: object) -> object:
    contract = importlib.import_module("workflows.email_feedback.repository")
    values: dict[str, object] = {
        "tenant_id": ids["tenant"],
        "nonce_sha256": b"n" * 32,
        "contact_point_id": ids["contact"],
        "message_attempt_id": ids["attempt"],
        "key_id": "primary-v1",
        "expires_at": NOW + timedelta(days=90),
        "consumed_at": None,
        "created_at": NOW,
    }
    values.update(changes)
    return contract.UnsubscribeTokenRecord(**values)


def test_feedback_repository_protocols_and_records_are_strict() -> None:
    """签名漂移、自由词表或危险字段会破坏后续 workflow 的唯一合同。"""
    shared = importlib.import_module("shared.schemas.email_feedback")
    contract = importlib.import_module("workflows.email_feedback.repository")
    assert {item.value for item in shared.EmailFeedbackKind} == {
        "hard_bounce",
        "soft_bounce",
        "unparseable",
    }
    assert {item.value for item in shared.EmailFeedbackResult} == {
        "applied",
        "recorded",
        "quarantined",
    }
    assert {item.value for item in shared.EmailFeedbackQuarantineReason} == {
        "malformed",
        "unsupported",
        "missing-correlation",
        "ambiguous-correlation",
        "cross-tenant-correlation",
    }
    expected = {
        (contract.FeedbackCursorRepository, "get"): (
            "self",
            "tenant_id",
            "mailbox_alias",
        ),
        (contract.FeedbackCursorRepository, "lock_expected"): (
            "self",
            "tenant_id",
            "mailbox_alias",
            "expected_cursor",
        ),
        (contract.FeedbackCursorRepository, "advance"): (
            "self",
            "cursor",
            "next_cursor",
            "at",
        ),
        (contract.FeedbackReceiptRepository, "append_if_absent"): (
            "self",
            "receipt",
        ),
        (contract.FeedbackReceiptRepository, "get"): (
            "self",
            "tenant_id",
            "mailbox_alias",
            "provider_event_id",
        ),
        (contract.UnsubscribeTokenRepository, "add"): ("self", "token"),
        (contract.UnsubscribeTokenRepository, "get_for_update"): (
            "self",
            "tenant_id",
            "nonce_sha256",
        ),
        (contract.UnsubscribeTokenRepository, "mark_consumed"): (
            "self",
            "record",
            "at",
        ),
    }
    for (owner, method_name), parameters in expected.items():
        method = getattr(owner, method_name)
        assert inspect.iscoroutinefunction(method)
        assert tuple(inspect.signature(method).parameters) == parameters

    ids = _ids()
    receipt = _receipt(ids)
    assert "address" not in receipt.__dataclass_fields__
    assert "payload" not in receipt.__dataclass_fields__
    assert "headers" not in receipt.__dataclass_fields__
    for changes in (
        {"mailbox_alias": "customer@example.com"},
        {"provider_event_id": "A" * 64},
        {"item_fingerprint": "short"},
        {"ordinal": 100},
        {"occurred_at": NOW.replace(tzinfo=None)},
        {"result": "applied"},
    ):
        with pytest.raises(ValidationError):
            _receipt(ids, **changes)
    for changes in (
        {"nonce_sha256": b"short"},
        {"key_id": "Bearer-secret"},
        {"expires_at": NOW + timedelta(days=89)},
        {"consumed_at": NOW + timedelta(days=90)},
    ):
        with pytest.raises(ValidationError):
            _token(ids, **changes)
    assert _token(ids, key_id="2026-v1").key_id == "2026-v1"

    opaque_cursor = "provider-cursor-private-marker"
    cursor = contract.FeedbackCursor(
        ids["tenant"],
        "feedback-primary",
        opaque_cursor,
        1,
        NOW,
        NOW,
    )
    assert cursor.provider_cursor == opaque_cursor
    assert opaque_cursor not in repr(cursor)


@pytest.mark.asyncio
async def test_cursor_is_tenant_bound_and_uses_versioned_cas(
    feedback_factory: async_sessionmaker[AsyncSession],
) -> None:
    repositories = importlib.import_module("infra.db.repositories.email_feedback")
    contract = importlib.import_module("workflows.email_feedback.repository")
    ids = _ids()
    async with feedback_factory() as session:
        repo = repositories.FeedbackCursorRepositoryImpl(
            session, ids["tenant"], now=lambda: NOW
        )
        cursor = await repo.lock_expected(
            ids["tenant"], "feedback-primary", None
        )
        assert cursor == contract.FeedbackCursor(
            ids["tenant"], "feedback-primary", None, 0, NOW, None
        )
        await repo.advance(cursor, "opaque-cursor-v1", NOW)
        await session.commit()

    async with feedback_factory() as session:
        repo = repositories.FeedbackCursorRepositoryImpl(
            session, ids["tenant"], now=lambda: NOW
        )
        stored = await repo.get(ids["tenant"], "feedback-primary")
        assert stored is not None
        assert (stored.provider_cursor, stored.version, stored.last_succeeded_at) == (
            "opaque-cursor-v1",
            1,
            NOW,
        )
        with pytest.raises(TransientError):
            await repo.lock_expected(
                ids["tenant"], "feedback-primary", "stale-cursor"
            )
        assert await repo.get(ids["other_tenant"], "feedback-primary") is None
        with pytest.raises(TenantIsolationViolation):
            await repo.lock_expected(
                ids["other_tenant"], "feedback-primary", None
            )


@pytest.mark.asyncio
async def test_receipt_append_distinguishes_created_existing_and_conflict(
    feedback_factory: async_sessionmaker[AsyncSession],
) -> None:
    repositories = importlib.import_module("infra.db.repositories.email_feedback")
    contract = importlib.import_module("workflows.email_feedback.repository")
    ids = _ids()
    await _seed_feedback_prerequisites(feedback_factory, ids)
    receipt = _receipt(ids)
    async with feedback_factory() as session:
        repo = repositories.FeedbackReceiptRepositoryImpl(session, ids["tenant"])
        created = await repo.append_if_absent(receipt)
        existing = await repo.append_if_absent(receipt)
        conflict = await repo.append_if_absent(
            _receipt(ids, ordinal=1, created_at=NOW + timedelta(seconds=1))
        )
        assert created.status is contract.FeedbackReceiptAppendStatus.CREATED
        assert existing.status is contract.FeedbackReceiptAppendStatus.EXISTING
        assert conflict.status is contract.FeedbackReceiptAppendStatus.CONFLICT
        assert created.winner == existing.winner == receipt
        assert conflict.winner is None
        await session.commit()

    async with feedback_factory() as session:
        repo = repositories.FeedbackReceiptRepositoryImpl(session, ids["tenant"])
        assert await repo.get(
            ids["tenant"], "feedback-primary", receipt.provider_event_id
        ) == receipt
        assert await repo.get(
            ids["other_tenant"], "feedback-primary", receipt.provider_event_id
        ) is None


@pytest.mark.asyncio
async def test_concurrent_receipt_append_has_one_created_and_nineteen_existing(
    feedback_factory: async_sessionmaker[AsyncSession],
) -> None:
    repositories = importlib.import_module("infra.db.repositories.email_feedback")
    contract = importlib.import_module("workflows.email_feedback.repository")
    ids = _ids()
    await _seed_feedback_prerequisites(feedback_factory, ids)
    receipt = _receipt(ids, provider_event_id="b" * 64)

    async def append_once() -> object:
        async with feedback_factory() as session:
            result = await repositories.FeedbackReceiptRepositoryImpl(
                session, ids["tenant"]
            ).append_if_absent(receipt)
            await session.commit()
            return result.status

    statuses = await asyncio.gather(*(append_once() for _ in range(20)))
    assert statuses.count(contract.FeedbackReceiptAppendStatus.CREATED) == 1
    assert statuses.count(contract.FeedbackReceiptAppendStatus.EXISTING) == 19


@pytest.mark.asyncio
async def test_same_provider_event_and_nonce_are_isolated_by_tenant(
    feedback_factory: async_sessionmaker[AsyncSession],
) -> None:
    repositories = importlib.import_module("infra.db.repositories.email_feedback")
    contract = importlib.import_module("workflows.email_feedback.repository")
    first = _ids()
    second = _ids()
    await _seed_feedback_prerequisites(feedback_factory, first)
    await _seed_feedback_prerequisites(feedback_factory, second)
    for ids in (first, second):
        async with feedback_factory() as session:
            receipt_result = await repositories.FeedbackReceiptRepositoryImpl(
                session, ids["tenant"]
            ).append_if_absent(_receipt(ids, provider_event_id="e" * 64))
            assert receipt_result.status is contract.FeedbackReceiptAppendStatus.CREATED
            await repositories.UnsubscribeTokenRepositoryImpl(
                session, ids["tenant"]
            ).add(_token(ids, nonce_sha256=b"z" * 32))
            await session.commit()

    for ids in (first, second):
        async with feedback_factory() as session:
            stored = await repositories.UnsubscribeTokenRepositoryImpl(
                session, ids["tenant"]
            ).get_for_update(ids["tenant"], b"z" * 32)
            assert stored is not None
            assert stored.message_attempt_id == ids["attempt"]


@pytest.mark.asyncio
async def test_quarantine_and_token_roundtrip_are_tenant_scoped(
    feedback_factory: async_sessionmaker[AsyncSession],
) -> None:
    repositories = importlib.import_module("infra.db.repositories.email_feedback")
    contract = importlib.import_module("workflows.email_feedback.repository")
    shared = importlib.import_module("shared.schemas.email_feedback")
    rows = importlib.import_module("infra.db.tables")
    ids = _ids()
    await _seed_feedback_prerequisites(feedback_factory, ids)
    receipt = _receipt(
        ids,
        provider_event_id="c" * 64,
        kind=shared.EmailFeedbackKind.UNPARSEABLE,
        result=shared.EmailFeedbackResult.QUARANTINED,
        attempt_id=None,
        enrollment_id=None,
        account_id=None,
        contact_point_id=None,
        sending_identity_id=None,
    )
    quarantine = contract.FeedbackQuarantine(
        tenant_id=ids["tenant"],
        mailbox_alias="feedback-primary",
        provider_event_id="c" * 64,
        reason=shared.EmailFeedbackQuarantineReason.MALFORMED,
        provider_ref_digest="d" * 64,
        created_at=NOW,
    )
    token = _token(ids, key_id="2026-v1")
    async with feedback_factory() as session:
        receipt_repo = repositories.FeedbackReceiptRepositoryImpl(
            session, ids["tenant"]
        )
        quarantine_repo = repositories.FeedbackQuarantineRepositoryImpl(
            session, ids["tenant"]
        )
        token_repo = repositories.UnsubscribeTokenRepositoryImpl(
            session, ids["tenant"]
        )
        await receipt_repo.append_if_absent(receipt)
        assert await quarantine_repo.append_if_absent(quarantine)
        assert not await quarantine_repo.append_if_absent(quarantine)
        await token_repo.add(token)
        locked = await token_repo.get_for_update(ids["tenant"], b"n" * 32)
        assert locked == token
        assert await token_repo.mark_consumed(locked, NOW + timedelta(days=1))
        assert not await token_repo.mark_consumed(locked, NOW + timedelta(days=2))
        assert (
            await token_repo.get_for_update(ids["other_tenant"], b"n" * 32)
            is None
        )
        await session.commit()
    async with feedback_factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(rows.EmailFeedbackReceiptRow)
            .where(rows.EmailFeedbackReceiptRow.tenant_id == ids["tenant"])
        ) == 1
        assert await session.scalar(
            select(func.count())
            .select_from(rows.EmailFeedbackQuarantineRow)
            .where(rows.EmailFeedbackQuarantineRow.tenant_id == ids["tenant"])
        ) == 1
        stored = await repositories.UnsubscribeTokenRepositoryImpl(
            session, ids["tenant"]
        ).get_for_update(ids["tenant"], b"n" * 32)
        assert stored is not None
        assert stored.consumed_at == NOW + timedelta(days=1)
        assert stored.key_id == "2026-v1"


@pytest.mark.asyncio
async def test_unsubscribe_token_database_guard_is_one_way_and_immutable(
    feedback_engine: AsyncEngine,
    feedback_factory: async_sessionmaker[AsyncSession],
) -> None:
    repositories = importlib.import_module("infra.db.repositories.email_feedback")
    ids = _ids()
    await _seed_feedback_prerequisites(feedback_factory, ids)
    token = _token(ids)
    async with feedback_factory() as session:
        repo = repositories.UnsubscribeTokenRepositoryImpl(session, ids["tenant"])
        await repo.add(token)
        locked = await repo.get_for_update(ids["tenant"], b"n" * 32)
        assert locked is not None
        assert await repo.mark_consumed(locked, NOW + timedelta(days=1))
        await session.commit()

    statements = (
        "DELETE FROM unsubscribe_tokens WHERE tenant_id=:tenant",
        "UPDATE unsubscribe_tokens SET consumed_at=NULL WHERE tenant_id=:tenant",
        "UPDATE unsubscribe_tokens SET key_id='rotated-v2' WHERE tenant_id=:tenant",
        "UPDATE unsubscribe_tokens SET consumed_at=:late WHERE tenant_id=:tenant",
    )
    for statement in statements:
        with pytest.raises(DBAPIError):
            async with feedback_engine.begin() as conn:
                await conn.execute(
                    text(statement),
                    {
                        "tenant": str(ids["tenant"]),
                        "late": NOW + timedelta(days=90),
                    },
                )

    invalid_inserts = (
        {
            "nonce": b"short",
            "created": NOW,
            "expires": NOW + timedelta(days=90),
        },
        {
            "nonce": b"x" * 32,
            "created": NOW,
            "expires": NOW + timedelta(days=89),
        },
    )
    for values in invalid_inserts:
        with pytest.raises(DBAPIError):
            async with feedback_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO unsubscribe_tokens "
                        "(tenant_id,nonce_sha256,contact_point_id,message_attempt_id,"
                        "key_id,expires_at,consumed_at,created_at) VALUES "
                        "(:tenant,:nonce,:contact,:attempt,'primary-v1',:expires,NULL,:created)"
                    ),
                    {
                        "tenant": str(ids["tenant"]),
                        "contact": str(ids["contact"]),
                        "attempt": str(ids["attempt"]),
                        **values,
                    },
                )
