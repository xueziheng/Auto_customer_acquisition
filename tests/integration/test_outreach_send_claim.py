"""Outreach 发送 claim 的真实 PostgreSQL 线性化与持久证据。"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    EnrollmentCreateRequest,
    SuppressionRequest,
    SuppressionTarget,
)
from shared.errors import TradeOSError
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)


async def _ready(
    db_url: str, *, suffix: str, claim_session_class=None
) -> tuple[object, object, object, object, object]:
    helpers = importlib.import_module("tests.integration.test_outreach_enrollment_lifecycle")
    concurrency = importlib.import_module("tests.integration.test_outreach_concurrency")
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign = CampaignId(new_id("cmp"))
    approval = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    snapshots = {(contact, account): concurrency._snapshot(tenant, contact, account)}
    await helpers._seed_active_campaign(factory, tenant, campaign, (sender,), approval)
    audit = helpers.FakeAudit(helpers.Trace())
    service = helpers._service(
        factory,
        tenant,
        campaign,
        approval,
        (sender,),
        snapshots,
        helpers.MutableClock(helpers.NOW),
        audit,
    )
    boss = Actor(f"boss:{suffix}", OutreachScope(level=ScopeLevel.TENANT), "boss")
    enrollment = await service.enroll(
        tenant,
        campaign,
        EnrollmentCreateRequest(account, contact, IdempotencyKey(f"claim-enroll-{suffix}")),
        actor=boss,
    )
    system = Actor(
        f"system:{suffix}",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
        ),
        "system",
    )
    attempt = await service.prepare_message_attempt(
        tenant, enrollment.enrollment_id, actor=system
    )
    if claim_session_class is not None:
        claim_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=claim_session_class,
        )
        service = helpers._service(
            claim_factory,
            tenant,
            campaign,
            approval,
            (sender,),
            snapshots,
            helpers.MutableClock(helpers.NOW),
            audit,
        )
    return engine, service, system, attempt, audit


async def test_twenty_claims_persist_one_sending_transition_and_action(db_url: str) -> None:
    engine, service, actor, attempt, _audit = await _ready(
        db_url, suffix="concurrent"
    )
    rows = importlib.import_module("infra.db.tables")
    try:
        claimed = await asyncio.gather(
            *(
                service.claim_message_send(
                    attempt.tenant_id, attempt.attempt_id, actor=actor
                )
                for _ in range(20)
            )
        )
        assert {view.state.value for view in claimed} == {"sending"}
        assert len({view.send_claimed_at for view in claimed}) == 1
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            stored = await session.get(
                rows.OutreachMessageAttemptRow,
                (str(attempt.tenant_id), str(attempt.attempt_id)),
            )
            assert stored is not None
            assert stored.state == "sending" and stored.send_claimed_at is not None
            assert await session.scalar(
                select(func.count())
                .select_from(rows.OutreachActionRow)
                .where(
                    rows.OutreachActionRow.tenant_id == attempt.tenant_id,
                    rows.OutreachActionRow.action_key
                    == f"attempt:{attempt.attempt_id}:sending",
                )
            ) == 1
    finally:
        await engine.dispose()


async def test_suppression_first_rejects_claim_but_claim_first_remains_sending(
    db_url: str,
) -> None:
    models = importlib.import_module("domains.outreach.models")
    rows = importlib.import_module("infra.db.tables")
    for order in ("suppression_first", "claim_first"):
        engine, service, actor, attempt, _audit = await _ready(db_url, suffix=order)
        # Attempt view intentionally has no account; authoritative enrollment supplies it.
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            enrollment = await session.get(
                rows.OutreachEnrollmentRow,
                (str(attempt.tenant_id), str(attempt.enrollment_id)),
            )
            assert enrollment is not None
            target = SuppressionTarget(account_id=enrollment.account_id)
        suppress_actor = Actor(
            f"system:suppress:{order}",
            OutreachScope(
                level=ScopeLevel.SYSTEM,
                allowed_suppression_targets=frozenset({target.canonical_id}),
            ),
            "system",
        )
        request = SuppressionRequest(
            target,
            models.SuppressionReason.UNSUBSCRIBE,
            importlib.import_module("tests.integration.test_outreach_enrollment_lifecycle").NOW,
            f"reply_ref_{order}",
            IdempotencyKey(f"suppression-{order}"),
        )
        try:
            if order == "suppression_first":
                await service.add_suppression(
                    attempt.tenant_id, request, actor=suppress_actor
                )
                try:
                    await service.claim_message_send(
                        attempt.tenant_id, attempt.attempt_id, actor=actor
                    )
                except TradeOSError:
                    pass
                else:
                    raise AssertionError("suppression-first 必须拒绝 claim")
                expected = "reserved"
            else:
                await service.claim_message_send(
                    attempt.tenant_id, attempt.attempt_id, actor=actor
                )
                await service.add_suppression(
                    attempt.tenant_id, request, actor=suppress_actor
                )
                expected = "sending"
            async with factory() as session:
                stored = await session.get(
                    rows.OutreachMessageAttemptRow,
                    (str(attempt.tenant_id), str(attempt.attempt_id)),
                )
                enrollment = await session.get(
                    rows.OutreachEnrollmentRow,
                    (str(attempt.tenant_id), str(attempt.enrollment_id)),
                )
                assert stored is not None and stored.state == expected
                assert enrollment is not None and enrollment.state == "stopped_suppressed"
        finally:
            await engine.dispose()


async def test_claim_commit_failure_rolls_back_state_action_and_allow_audit(
    db_url: str,
) -> None:
    class CommitFailure(RuntimeError):
        pass

    commits = 0

    class SecondCommitFails(AsyncSession):
        async def commit(self) -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                raise CommitFailure
            await super().commit()

    engine, service, actor, attempt, audit = await _ready(
        db_url,
        suffix="commit-failure",
        claim_session_class=SecondCommitFails,
    )
    rows = importlib.import_module("infra.db.tables")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    allow_before = sum(
        record["rule"].startswith("allow:") for record in audit.records
    )
    try:
        with pytest.raises(CommitFailure):
            await service.claim_message_send(
                attempt.tenant_id, attempt.attempt_id, actor=actor
            )
        async with factory() as session:
            stored = await session.get(
                rows.OutreachMessageAttemptRow,
                (str(attempt.tenant_id), str(attempt.attempt_id)),
            )
            actions = await session.scalar(
                select(func.count())
                .select_from(rows.OutreachActionRow)
                .where(
                    rows.OutreachActionRow.tenant_id == attempt.tenant_id,
                    rows.OutreachActionRow.action_key
                    == f"attempt:{attempt.attempt_id}:sending",
                )
            )
        assert stored is not None and stored.state == "reserved"
        assert stored.send_claimed_at is None
        assert actions == 0
        assert sum(
            record["rule"].startswith("allow:") for record in audit.records
        ) == allow_before
    finally:
        await engine.dispose()
