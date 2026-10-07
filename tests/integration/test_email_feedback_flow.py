"""真实 PostgreSQL 邮件反馈整页原子性、重放与并发。

投诉用例使用独立 module 级容器：complaint receipt（仅 0017 词表允许）若留在
共享 session 容器中，会破坏其后迁移测试对 0017 以下版本的 downgrade
（check constraint 重建会校验全表既有行）。隔离后共享容器始终保持
「已提交行在全部历史版本均合法」的不变量。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from shared.errors import TransientError
from shared.schemas.email_feedback import (
    EmailFeedbackCorrelation,
    EmailFeedbackItem,
    EmailFeedbackKind,
    EmailFeedbackPage,
    EmailFeedbackParseIssue,
)
from shared.schemas.identifiers import TenantId, new_id

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
_COMPLAINT_IMAGE = "pgvector/pgvector:pg16"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def feedback_flow_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _to_asyncpg(url: str) -> str:
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise ValueError("未知连接串 scheme：只接受 postgresql 方言")


@pytest.fixture(scope="module")
def complaint_postgres() -> Iterator[PostgresContainer]:
    """投诉用例专用容器：complaint receipt 不进入共享 session 数据库。"""
    if shutil.which("docker") is None:
        pytest.skip("Docker 不可用")
    with PostgresContainer(_COMPLAINT_IMAGE) as pg:
        yield pg


@pytest.fixture(scope="module")
def complaint_db_url(complaint_postgres: PostgresContainer) -> str:
    url = _to_asyncpg(complaint_postgres.get_connection_url())
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", "upgrade", "head"],
        capture_output=True,
        check=False,
        env=env,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, "alembic upgrade head 失败（不输出连接内容）"
    return url


@pytest_asyncio.fixture
async def complaint_flow_engine(complaint_db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(
        complaint_db_url
    )
    try:
        yield engine
    finally:
        await engine.dispose()


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def log(self, **record: object) -> None:
        self.records.append(dict(record))


def _outreach_builder(tenant: TenantId, now: Callable[[], datetime]) -> object:
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


def _sending_builder(tenant: TenantId, now: Callable[[], datetime]) -> object:
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


def _outreach_actor(identity_id: str) -> object:
    permissions = importlib.import_module("domains.outreach.permissions")
    return permissions.Actor(
        "system:feedback",
        permissions.OutreachScope(
            level=permissions.ScopeLevel.SYSTEM,
            allowed_sending_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _sending_actor(identity_id: str) -> object:
    permissions = importlib.import_module("domains.sending_identity.permissions")
    return permissions.Actor(
        "system:feedback",
        permissions.SendingIdentityScope(
            level=permissions.ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _ids() -> dict[str, object]:
    return {
        "tenant": TenantId(new_id("tn")),
        "identity": new_id("sid"),
        "campaign": new_id("cmp"),
        "enrollments": (new_id("enr"), new_id("enr")),
        "attempts": (new_id("mat"), new_id("mat")),
        "accounts": (new_id("acc"), new_id("acc")),
        "contacts": (new_id("cp"), new_id("cp")),
    }


async def _seed(
    factory: async_sessionmaker[AsyncSession], ids: dict[str, object]
) -> None:
    rows = importlib.import_module("infra.db.tables")
    tenant = str(ids["tenant"])
    identity = str(ids["identity"])
    campaign = str(ids["campaign"])
    async with factory() as session:
        session.add(
            rows.SendingDomainRow(
                tenant_id=tenant,
                domain="feedback-flow.example",
                role="cold_outreach",
                created_at=NOW - timedelta(days=40),
            )
        )
        await session.flush()
        session.add(
            rows.SendingIdentityRow(
                tenant_id=tenant,
                identity_id=identity,
                domain="feedback-flow.example",
                address="feedback@feedback-flow.example",
                display_name=None,
                state="active",
                connector_ref="feedback_connector_ref",
                warmup_started_on=(NOW - timedelta(days=40)).date(),
                target_daily_volume=50,
                activated_at=NOW - timedelta(days=10),
                suspended_at=None,
                retired_at=None,
                sendable_state_before_restriction=None,
                suspension_category=None,
                version=1,
                throttle_hard_bounce_rate=Decimal("0.03"),
                suspend_hard_bounce_rate=Decimal("0.05"),
                throttle_complaint_rate=Decimal("0.001"),
                suspend_complaint_rate=Decimal("0.003"),
                suspend_on_spam_trap=True,
                suspend_on_blocklist=True,
                minimum_sample=50,
                created_at=NOW - timedelta(days=40),
            )
        )
        session.add(
            rows.OutreachCampaignRow(
                tenant_id=tenant,
                campaign_id=campaign,
                state="active",
                current_version=1,
                created_by=new_id("emp"),
                created_at=NOW - timedelta(days=2),
                round_robin_cursor=0,
                approval_id=new_id("apr"),
                approved_by=new_id("emp"),
                approved_at=NOW - timedelta(days=2),
                paused_reason=None,
            )
        )
        await session.flush()
        session.add(
            rows.OutreachCampaignVersionRow(
                tenant_id=tenant,
                campaign_id=campaign,
                version=1,
                name="feedback flow fixture",
                markets=["US"],
                target_entity_types=["business"],
                allowed_categories=["hinges"],
                sender_identity_ids=[identity],
                daily_new_contact_limit=10,
                daily_total_message_limit=20,
                handoff_triggers=["reply"],
                stop_on_reply=True,
                created_by=new_id("emp"),
                created_at=NOW - timedelta(days=2),
            )
        )
        await session.flush()
        for index in range(2):
            enrollment = str(ids["enrollments"][index])
            attempt = str(ids["attempts"][index])
            digest = chr(ord("a") + index) * 64
            session.add(
                rows.OutreachEnrollmentRow(
                    tenant_id=tenant,
                    enrollment_id=enrollment,
                    campaign_id=campaign,
                    campaign_version=1,
                    account_id=str(ids["accounts"][index]),
                    contact_point_id=str(ids["contacts"][index]),
                    sending_identity_id=identity,
                    state="in_sequence",
                    current_step=1,
                    next_send_at=NOW + timedelta(days=1),
                    enrolled_at=NOW - timedelta(days=1),
                    stopped_at=None,
                    stop_reason=None,
                    idempotency_key=f"feedback-enrollment-{index}",
                )
            )
            await session.flush()
            session.add(
                rows.OutreachMessageAttemptRow(
                    tenant_id=tenant,
                    attempt_id=attempt,
                    message_id=new_id("msg"),
                    campaign_id=campaign,
                    enrollment_id=enrollment,
                    campaign_version=1,
                    step_number=1,
                    sending_identity_id=identity,
                    idempotency_key=f"feedback-attempt-{index}",
                    state="sent",
                    provider_ref=f"provider_ref_{index}",
                    failure_category=None,
                    created_at=NOW - timedelta(hours=1),
                    updated_at=NOW - timedelta(hours=1),
                    send_claimed_at=NOW - timedelta(hours=1),
                    deterministic_message_id=(
                        f"<route-v1.{digest}@messages.tradeos.invalid>"
                    ),
                    idempotency_header=f"route-v1.{digest}",
                )
            )
        await session.commit()


def _correlation(index: int) -> EmailFeedbackCorrelation:
    digest = chr(ord("a") + index) * 64
    return EmailFeedbackCorrelation(
        "route-v1",
        f"route-v1.{digest}@messages.tradeos.invalid",
        f"route-v1.{digest}",
    )


def _page(start: str | None, next_cursor: str) -> EmailFeedbackPage:
    return EmailFeedbackPage(
        start,
        next_cursor,
        (
            EmailFeedbackItem(
                "c" * 64,
                "1" * 64,
                0,
                EmailFeedbackKind.HARD_BOUNCE,
                NOW,
                _correlation(0),
                None,
            ),
            EmailFeedbackItem(
                "d" * 64,
                "2" * 64,
                1,
                EmailFeedbackKind.SOFT_BOUNCE,
                NOW,
                _correlation(1),
                None,
            ),
            EmailFeedbackItem(
                "e" * 64,
                "3" * 64,
                2,
                EmailFeedbackKind.UNPARSEABLE,
                NOW,
                None,
                EmailFeedbackParseIssue.MALFORMED,
            ),
        ),
    )


def _processor(
    factory: async_sessionmaker[AsyncSession], ids: dict[str, object], audit: _Audit
) -> object:
    module = importlib.import_module("workflows.email_feedback.flow")
    uow_type = importlib.import_module(
        "infra.db.email_feedback_uow"
    ).SqlAlchemyFeedbackPageUnitOfWork
    tenant = ids["tenant"]

    def uow_factory(requested: TenantId) -> object:
        assert requested == tenant
        return uow_type(
            factory,
            tenant,
            outreach_builder=_outreach_builder(tenant, lambda: NOW),
            sending_identity_builder=_sending_builder(tenant, lambda: NOW),
            audit_sink=audit,
            now=lambda: NOW,
        )

    return module.FeedbackPageProcessor(
        uow_factory,
        route_id="route-v1",
        outreach_actor_factory=_outreach_actor,
        sending_identity_actor_factory=_sending_actor,
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_real_page_commits_receipts_quarantine_domain_effects_and_cursor(
    feedback_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(feedback_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()
    result = await _processor(factory, ids, audit).process(
        ids["tenant"], "feedback-primary", ids["identity"], None, _page(None, "v1")
    )
    assert (result.processed, result.hard_bounces, result.soft_bounces) == (3, 1, 1)
    assert (result.quarantined, result.duplicates) == (1, 0)
    async with factory() as session:
        receipts = (
            (
                await session.execute(
                    select(rows.EmailFeedbackReceiptRow).where(
                        rows.EmailFeedbackReceiptRow.tenant_id == ids["tenant"]
                    )
                )
            )
            .scalars()
            .all()
        )
        assert Counter(row.result for row in receipts) == {
            "applied": 1,
            "recorded": 1,
            "quarantined": 1,
        }
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.EmailFeedbackQuarantineRow)
                .where(rows.EmailFeedbackQuarantineRow.tenant_id == ids["tenant"])
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.OutreachSuppressionRow)
                .where(rows.OutreachSuppressionRow.tenant_id == ids["tenant"])
            )
            == 1
        )
        reputation_event = (
            (
                await session.execute(
                    select(rows.ReputationEventRow).where(
                        rows.ReputationEventRow.tenant_id == ids["tenant"]
                    )
                )
            )
            .scalars()
            .one()
        )
        assert (
            reputation_event.event_type,
            reputation_event.identity_id,
            reputation_event.occurred_at,
            reputation_event.dedup_key,
            reputation_event.source_ref,
        ) == (
            "hard_bounced",
            str(ids["identity"]),
            NOW,
            "c" * 64,
            "feedback_ZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGA",
        )
        first_enrollment = await session.get(
            rows.OutreachEnrollmentRow,
            (str(ids["tenant"]), str(ids["enrollments"][0])),
        )
        second_enrollment = await session.get(
            rows.OutreachEnrollmentRow,
            (str(ids["tenant"]), str(ids["enrollments"][1])),
        )
        assert first_enrollment.state == "stopped_bounced"
        assert second_enrollment.state == "in_sequence"
        cursor = await session.get(
            rows.EmailFeedbackCursorRow,
            (str(ids["tenant"]), "feedback-primary"),
        )
        assert (cursor.provider_cursor, cursor.version) == ("v1", 1)
    assert len(audit.records) == 5
    assert Counter(
        (
            record["actor"],
            record["action"],
            record["tenant_id"],
            record["scope"],
            record["rule"],
        )
        for record in audit.records
    ) == {
        (
            "system:feedback",
            "delivery_feedback:resolve",
            ids["tenant"],
            "system",
            "phase1:system:system:delivery_feedback:resolve",
        ): 2,
        (
            "system:feedback",
            "identity:read",
            ids["tenant"],
            "system",
            "phase1:system:system:identity:read",
        ): 1,
        (
            "system:feedback",
            "delivery_event:record",
            ids["tenant"],
            "system",
            "phase1:system:system:delivery_event:record",
        ): 1,
        (
            "system:feedback",
            "hard_bounce:apply",
            ids["tenant"],
            "system",
            "phase1:system:system:hard_bounce:apply",
        ): 1,
    }


@pytest.mark.asyncio
async def test_real_page_replay_is_domain_noop_and_does_not_duplicate_audit(
    feedback_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(feedback_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()
    await _processor(factory, ids, audit).process(
        ids["tenant"], "feedback-primary", ids["identity"], None, _page(None, "v1")
    )
    before_audit = len(audit.records)
    replay = await _processor(factory, ids, audit).process(
        ids["tenant"],
        "feedback-primary",
        ids["identity"],
        "v1",
        _page("v1", "v1"),
    )
    assert replay.duplicates == 3
    assert replay.processed == 0
    assert len(audit.records) == before_audit
    async with factory() as session:
        for row_type, count in (
            (rows.EmailFeedbackReceiptRow, 3),
            (rows.EmailFeedbackQuarantineRow, 1),
            (rows.OutreachSuppressionRow, 1),
            (rows.ReputationEventRow, 1),
        ):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(row_type)
                    .where(row_type.tenant_id == ids["tenant"])
                )
                == count
            )


@pytest.mark.asyncio
async def test_legacy_fingerprint_fails_closed_before_domain_read(
    feedback_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(feedback_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    async with factory() as session:
        session.add(
            rows.EmailFeedbackCursorRow(
                tenant_id=str(ids["tenant"]),
                mailbox_alias="feedback-primary",
                provider_cursor=None,
                version=0,
                bootstrap_started_at=NOW,
                last_succeeded_at=None,
            )
        )
        await session.flush()
        session.add(
            rows.EmailFeedbackReceiptRow(
                tenant_id=str(ids["tenant"]),
                mailbox_alias="feedback-primary",
                provider_event_id="c" * 64,
                item_fingerprint="0" * 64,
                ordinal=0,
                kind="hard_bounce",
                occurred_at=NOW,
                result="applied",
                attempt_id=str(ids["attempts"][0]),
                enrollment_id=str(ids["enrollments"][0]),
                account_id=str(ids["accounts"][0]),
                contact_point_id=str(ids["contacts"][0]),
                sending_identity_id=str(ids["identity"]),
                created_at=NOW,
            )
        )
        await session.commit()

    audit = _Audit()
    with pytest.raises(importlib.import_module("shared.errors").ValidationError):
        await _processor(factory, ids, audit).process(
            ids["tenant"],
            "feedback-primary",
            ids["identity"],
            None,
            EmailFeedbackPage(
                None,
                "v1",
                (
                    EmailFeedbackItem(
                        "c" * 64,
                        "1" * 64,
                        0,
                        EmailFeedbackKind.HARD_BOUNCE,
                        NOW,
                        _correlation(0),
                        None,
                    ),
                ),
            ),
        )

    assert audit.records == []
    async with factory() as session:
        cursor = await session.get(
            rows.EmailFeedbackCursorRow,
            (str(ids["tenant"]), "feedback-primary"),
        )
        assert (cursor.provider_cursor, cursor.version) == (None, 0)
        assert await session.scalar(
            select(func.count())
            .select_from(rows.ReputationEventRow)
            .where(rows.ReputationEventRow.tenant_id == ids["tenant"])
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachSuppressionRow)
            .where(rows.OutreachSuppressionRow.tenant_id == ids["tenant"])
        ) == 0


@pytest.mark.asyncio
async def test_real_domain_failure_rolls_back_receipts_effects_and_cursor(
    feedback_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(feedback_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()
    processor = _processor(factory, ids, audit)
    original_builder = _sending_builder(ids["tenant"], lambda: NOW)
    sentinel = RuntimeError("fixed sending repository failure")

    class FailingSending:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        async def record_delivery_event(
            self, *_args: object, **_kwargs: object
        ) -> bool:
            raise sentinel

    def failing_builder(bound_factory: object, sink: object) -> object:
        return FailingSending(original_builder(bound_factory, sink))

    uow_type = importlib.import_module(
        "infra.db.email_feedback_uow"
    ).SqlAlchemyFeedbackPageUnitOfWork
    processor._uow_factory = lambda _tenant: uow_type(
        factory,
        ids["tenant"],
        outreach_builder=_outreach_builder(ids["tenant"], lambda: NOW),
        sending_identity_builder=failing_builder,
        audit_sink=audit,
        now=lambda: NOW,
    )
    with pytest.raises(RuntimeError) as raised:
        await processor.process(
            ids["tenant"],
            "feedback-primary",
            ids["identity"],
            None,
            _page(None, "v1"),
        )
    assert raised.value is sentinel
    assert audit.records == []
    async with factory() as session:
        for row_type in (
            rows.EmailFeedbackCursorRow,
            rows.EmailFeedbackReceiptRow,
            rows.EmailFeedbackQuarantineRow,
            rows.OutreachSuppressionRow,
            rows.ReputationEventRow,
        ):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(row_type)
                    .where(row_type.tenant_id == ids["tenant"])
                )
                == 0
            )


@pytest.mark.asyncio
async def test_same_page_twenty_way_commits_once_and_retry_uses_durable_receipts(
    feedback_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(feedback_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()

    async def attempt() -> object:
        try:
            return await _processor(factory, ids, audit).process(
                ids["tenant"],
                "feedback-primary",
                ids["identity"],
                None,
                _page(None, "v1"),
            )
        except TransientError as exc:
            return exc

    outcomes = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    assert sum(isinstance(value, TransientError) for value in outcomes) == 19
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.EmailFeedbackReceiptRow)
                .where(rows.EmailFeedbackReceiptRow.tenant_id == ids["tenant"])
            )
            == 3
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.ReputationEventRow)
                .where(rows.ReputationEventRow.tenant_id == ids["tenant"])
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.OutreachSuppressionRow)
                .where(rows.OutreachSuppressionRow.tenant_id == ids["tenant"])
            )
            == 1
        )


@pytest.mark.asyncio
async def test_two_mailboxes_with_inverse_contact_order_complete_without_deadlock(
    feedback_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(feedback_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()

    def hard(event: str, index: int, ordinal: int) -> EmailFeedbackItem:
        return EmailFeedbackItem(
            event * 64,
            event * 64,
            ordinal,
            EmailFeedbackKind.HARD_BOUNCE,
            NOW,
            _correlation(index),
            None,
        )

    first = _processor(factory, ids, audit).process(
        ids["tenant"],
        "feedback-primary",
        ids["identity"],
        None,
        EmailFeedbackPage(None, "primary-v1", (hard("c", 0, 0), hard("d", 1, 1))),
    )
    second = _processor(factory, ids, audit).process(
        ids["tenant"],
        "feedback-secondary",
        ids["identity"],
        None,
        EmailFeedbackPage(None, "secondary-v1", (hard("f", 1, 0), hard("a", 0, 1))),
    )
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    assert [(item.processed, item.hard_bounces) for item in results] == [(2, 2), (2, 2)]

    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.EmailFeedbackReceiptRow)
                .where(rows.EmailFeedbackReceiptRow.tenant_id == ids["tenant"])
            )
            == 4
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.ReputationEventRow)
                .where(rows.ReputationEventRow.tenant_id == ids["tenant"])
            )
            == 4
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(rows.OutreachSuppressionRow)
                .where(rows.OutreachSuppressionRow.tenant_id == ids["tenant"])
            )
            == 4
        )
        cursors = (
            (
                await session.execute(
                    select(rows.EmailFeedbackCursorRow).where(
                        rows.EmailFeedbackCursorRow.tenant_id == ids["tenant"]
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.mailbox_alias, row.provider_cursor, row.version) for row in cursors
        } == {
            ("feedback-primary", "primary-v1", 1),
            ("feedback-secondary", "secondary-v1", 1),
        }


def _complaint_item(*, event: str = "c" * 64, ordinal: int = 0, index: int = 0) -> EmailFeedbackItem:
    return EmailFeedbackItem(
        event,
        event,
        ordinal,
        EmailFeedbackKind.COMPLAINT,
        NOW,
        _correlation(index),
        None,
    )


def _complaint_page(
    start: str | None, next_cursor: str, *, event: str = "c" * 64
) -> EmailFeedbackPage:
    return EmailFeedbackPage(start, next_cursor, (_complaint_item(event=event),))


@pytest.mark.asyncio
async def test_real_complaint_page_commits_all_domain_effects_and_outbox(
    complaint_flow_engine: AsyncEngine,
) -> None:
    """单事务：complaint receipt、抑制、停 enrollment、信誉事件、outbox 与 cursor。"""
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(complaint_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()
    event = "c" * 64
    result = await _processor(factory, ids, audit).process(
        ids["tenant"],
        "feedback-primary",
        ids["identity"],
        None,
        _complaint_page(None, "complaint-v1", event=event),
    )
    assert (
        result.processed,
        result.complaints,
        result.hard_bounces,
        result.soft_bounces,
        result.quarantined,
        result.duplicates,
    ) == (1, 1, 0, 0, 0, 0)
    async with factory() as session:
        receipt = (
            (
                await session.execute(
                    select(rows.EmailFeedbackReceiptRow).where(
                        rows.EmailFeedbackReceiptRow.tenant_id == ids["tenant"]
                    )
                )
            )
            .scalars()
            .one()
        )
        assert (receipt.kind, receipt.result) == ("complaint", "applied")
        assert receipt.attempt_id == str(ids["attempts"][0])
        assert receipt.contact_point_id == str(ids["contacts"][0])
        suppression = (
            (
                await session.execute(
                    select(rows.OutreachSuppressionRow).where(
                        rows.OutreachSuppressionRow.tenant_id == ids["tenant"]
                    )
                )
            )
            .scalars()
            .one()
        )
        assert (
            suppression.reason,
            suppression.contact_point_id,
            suppression.account_id,
            suppression.source_ref,
        ) == ("complaint", str(ids["contacts"][0]), None, event)
        first_enrollment = await session.get(
            rows.OutreachEnrollmentRow,
            (str(ids["tenant"]), str(ids["enrollments"][0])),
        )
        second_enrollment = await session.get(
            rows.OutreachEnrollmentRow,
            (str(ids["tenant"]), str(ids["enrollments"][1])),
        )
        assert (
            first_enrollment.state,
            first_enrollment.stop_reason,
            first_enrollment.next_send_at,
        ) == ("stopped_suppressed", "suppression", None)
        assert (second_enrollment.state, second_enrollment.stop_reason) == (
            "in_sequence",
            None,
        )
        reputation = (
            (
                await session.execute(
                    select(rows.ReputationEventRow).where(
                        rows.ReputationEventRow.tenant_id == ids["tenant"]
                    )
                )
            )
            .scalars()
            .one()
        )
        assert (
            reputation.event_type,
            reputation.identity_id,
            reputation.occurred_at,
            reputation.dedup_key,
            reputation.source_ref,
        ) == (
            "complaint",
            str(ids["identity"]),
            NOW,
            event,
            "feedback_ZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGMZTGA",
        )
        outbox = (
            (
                await session.execute(
                    select(rows.OutboxEventRow).where(
                        rows.OutboxEventRow.tenant_id == ids["tenant"],
                        rows.OutboxEventRow.event_type == "ComplaintReceived",
                    )
                )
            )
            .scalars()
            .one()
        )
        assert outbox.status == "pending"
        payload = outbox.event_payload
        assert payload["tenant_id"] == str(ids["tenant"])
        assert payload["message_attempt_id"] == str(ids["attempts"][0])
        assert payload["sending_identity_id"] == str(ids["identity"])
        assert payload["dedup_key"] == event
        cursor = await session.get(
            rows.EmailFeedbackCursorRow,
            (str(ids["tenant"]), "feedback-primary"),
        )
        assert (cursor.provider_cursor, cursor.version) == ("complaint-v1", 1)
    assert len(audit.records) == 4
    assert Counter(
        (
            record["actor"],
            record["action"],
            record["tenant_id"],
            record["scope"],
            record["rule"],
        )
        for record in audit.records
    ) == {
        (
            "system:feedback",
            "delivery_feedback:resolve",
            ids["tenant"],
            "system",
            "phase1:system:system:delivery_feedback:resolve",
        ): 1,
        (
            "system:feedback",
            "identity:read",
            ids["tenant"],
            "system",
            "phase1:system:system:identity:read",
        ): 1,
        (
            "system:feedback",
            "delivery_event:record",
            ids["tenant"],
            "system",
            "phase1:system:system:delivery_event:record",
        ): 1,
        (
            "system:feedback",
            "complaint:apply",
            ids["tenant"],
            "system",
            "phase1:system:system:complaint:apply",
        ): 1,
    }


@pytest.mark.asyncio
async def test_real_complaint_replay_is_noop_and_does_not_duplicate_audit(
    complaint_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(complaint_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()
    await _processor(factory, ids, audit).process(
        ids["tenant"],
        "feedback-primary",
        ids["identity"],
        None,
        _complaint_page(None, "complaint-v1"),
    )
    before_audit = len(audit.records)
    replay = await _processor(factory, ids, audit).process(
        ids["tenant"],
        "feedback-primary",
        ids["identity"],
        "complaint-v1",
        _complaint_page("complaint-v1", "complaint-v1"),
    )
    assert (replay.duplicates, replay.processed, replay.complaints) == (1, 0, 0)
    assert len(audit.records) == before_audit
    async with factory() as session:
        for row_type, count in (
            (rows.EmailFeedbackReceiptRow, 1),
            (rows.OutreachSuppressionRow, 1),
            (rows.ReputationEventRow, 1),
            (rows.OutboxEventRow, 1),
        ):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(row_type)
                    .where(row_type.tenant_id == ids["tenant"])
                )
                == count
            )


@pytest.mark.asyncio
async def test_complaint_same_event_different_fingerprint_fails_before_domain_call(
    complaint_flow_engine: AsyncEngine,
) -> None:
    """同 event ID 异 payload 必须在调用任何域服务前整页失败。"""
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(complaint_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    async with factory() as session:
        session.add(
            rows.EmailFeedbackCursorRow(
                tenant_id=str(ids["tenant"]),
                mailbox_alias="feedback-primary",
                provider_cursor=None,
                version=0,
                bootstrap_started_at=NOW,
                last_succeeded_at=None,
            )
        )
        await session.flush()
        session.add(
            rows.EmailFeedbackReceiptRow(
                tenant_id=str(ids["tenant"]),
                mailbox_alias="feedback-primary",
                provider_event_id="c" * 64,
                item_fingerprint="0" * 64,
                ordinal=0,
                kind="complaint",
                occurred_at=NOW,
                result="applied",
                attempt_id=str(ids["attempts"][0]),
                enrollment_id=str(ids["enrollments"][0]),
                account_id=str(ids["accounts"][0]),
                contact_point_id=str(ids["contacts"][0]),
                sending_identity_id=str(ids["identity"]),
                created_at=NOW,
            )
        )
        await session.commit()

    audit = _Audit()
    with pytest.raises(importlib.import_module("shared.errors").ValidationError):
        await _processor(factory, ids, audit).process(
            ids["tenant"],
            "feedback-primary",
            ids["identity"],
            None,
            _complaint_page(None, "complaint-v1"),
        )

    assert audit.records == []
    async with factory() as session:
        cursor = await session.get(
            rows.EmailFeedbackCursorRow,
            (str(ids["tenant"]), "feedback-primary"),
        )
        assert (cursor.provider_cursor, cursor.version) == (None, 0)
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(rows.OutboxEventRow.tenant_id == ids["tenant"])
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachSuppressionRow)
            .where(rows.OutreachSuppressionRow.tenant_id == ids["tenant"])
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.ReputationEventRow)
            .where(rows.ReputationEventRow.tenant_id == ids["tenant"])
        ) == 0


@pytest.mark.asyncio
async def test_complaint_sending_domain_failure_rolls_back_every_write_and_cursor(
    complaint_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(complaint_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()
    sentinel = RuntimeError("fixed complaint sending repository failure")
    original_builder = _sending_builder(ids["tenant"], lambda: NOW)

    class FailingSending:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        async def record_delivery_event(
            self, *_args: object, **_kwargs: object
        ) -> bool:
            raise sentinel

    def failing_builder(bound_factory: object, sink: object) -> object:
        return FailingSending(original_builder(bound_factory, sink))

    uow_type = importlib.import_module(
        "infra.db.email_feedback_uow"
    ).SqlAlchemyFeedbackPageUnitOfWork
    processor = _processor(factory, ids, audit)
    processor._uow_factory = lambda _tenant: uow_type(
        factory,
        ids["tenant"],
        outreach_builder=_outreach_builder(ids["tenant"], lambda: NOW),
        sending_identity_builder=failing_builder,
        audit_sink=audit,
        now=lambda: NOW,
    )
    with pytest.raises(RuntimeError) as raised:
        await processor.process(
            ids["tenant"],
            "feedback-primary",
            ids["identity"],
            None,
            _complaint_page(None, "complaint-v1"),
        )
    assert raised.value is sentinel
    assert audit.records == []
    async with factory() as session:
        for row_type in (
            rows.EmailFeedbackCursorRow,
            rows.EmailFeedbackReceiptRow,
            rows.OutreachSuppressionRow,
            rows.ReputationEventRow,
            rows.OutboxEventRow,
        ):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(row_type)
                    .where(row_type.tenant_id == ids["tenant"])
                )
                == 0
            )


@pytest.mark.asyncio
async def test_complaint_outreach_domain_failure_rolls_back_every_write_and_cursor(
    complaint_flow_engine: AsyncEngine,
) -> None:
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(complaint_flow_engine, expire_on_commit=False)
    ids = _ids()
    await _seed(factory, ids)
    audit = _Audit()
    sentinel = RuntimeError("fixed complaint outreach repository failure")
    original_builder = _outreach_builder(ids["tenant"], lambda: NOW)

    class FailingOutreach:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        async def apply_complaint(
            self, *_args: object, **_kwargs: object
        ) -> object:
            raise sentinel

    def failing_builder(bound_factory: object, sink: object) -> object:
        return FailingOutreach(original_builder(bound_factory, sink))

    uow_type = importlib.import_module(
        "infra.db.email_feedback_uow"
    ).SqlAlchemyFeedbackPageUnitOfWork
    processor = _processor(factory, ids, audit)
    processor._uow_factory = lambda _tenant: uow_type(
        factory,
        ids["tenant"],
        outreach_builder=failing_builder,
        sending_identity_builder=_sending_builder(ids["tenant"], lambda: NOW),
        audit_sink=audit,
        now=lambda: NOW,
    )
    with pytest.raises(RuntimeError) as raised:
        await processor.process(
            ids["tenant"],
            "feedback-primary",
            ids["identity"],
            None,
            _complaint_page(None, "complaint-v1"),
        )
    assert raised.value is sentinel
    assert audit.records == []
    async with factory() as session:
        for row_type in (
            rows.EmailFeedbackCursorRow,
            rows.EmailFeedbackReceiptRow,
            rows.OutreachSuppressionRow,
            rows.ReputationEventRow,
            rows.OutboxEventRow,
        ):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(row_type)
                    .where(row_type.tenant_id == ids["tenant"])
                )
                == 0
            )
