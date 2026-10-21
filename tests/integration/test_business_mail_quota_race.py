"""真实SQL与model.generate竞争：预览ready后抢槽/次数，无外部模型派发必须仍待识别。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from apps.email_feedback_worker.business_mail import BusinessMailDriver
from infra.db.advisory_lock import PostgresAdvisoryLock, derive_advisory_lock_key
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.tables import ModelInvocationRow, ToolCallRow
from shared.business_mail import BusinessArchivedPage
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelUsage,
)
from tests.integration.test_business_mail_gateway import configured_model


@pytest.mark.parametrize("race", ["concurrency", "window"])
async def test_quota_race_with_zero_dispatch_stays_pending_then_classifies_once(
    integration_engine, monkeypatch, race
):
    # 只控制固定UTC时钟以走下一真实账本窗口；不更新/删除任何计量或工具调用记录。
    stamp = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp[0] if tz is None else stamp[0].astimezone(tz)

    monkeypatch.setattr("infra.db.business_mail.datetime", Clock)
    monkeypatch.setattr("tool_gateway.handlers.model_generate.datetime", Clock)
    (
        sessions,
        actor,
        _,
        repo,
        mailbox,
        _,
        _,
        classifier,
        provider,
    ) = await configured_model(
        integration_engine,
        claim_message=False,
        policy_changes={
            "tenant_calls": 1 if race == "window" else 3,
            "employee_calls": 1 if race == "window" else 3,
        },
    )
    generator = classifier.generator
    policy = generator._limits
    competing = SqlModelUsageRepository(sessions)
    competitor = InvocationIdentity(
        **actor.model_dump(),
        run_id=new_id("run"),
        capability="product_help",
        configuration_version="v1",
        sequence=0,
    )
    reservation = []

    class RacingUsage(SqlModelUsageRepository):
        async def reserve(self, identity, *args, **kwargs):
            if identity.capability == "business_mail_triage" and not reservation:
                # 原预览已返回ready；在Gateway最终原子reserve之前另一真实事务抢占。
                other = await competing.reserve(
                    competitor, "b" * 64, policy, stamp[0], model="fixture"
                )
                assert other.outcome == "reserved"
                reservation.append(other)
                if race == "window":
                    await competing.mark_dispatched(
                        actor.tenant_id, other.invocation_id
                    )
                    await competing.finish(
                        actor.tenant_id,
                        other.invocation_id,
                        ModelUsage(
                            input_tokens=1, cached_input_tokens=0, output_tokens=1
                        ),
                        "succeeded",
                    )
            return await super().reserve(identity, *args, **kwargs)

    generator._usage = RacingUsage(sessions)

    class NoNewMail:
        async def fetch(self, **kwargs):
            checkpoint = await repo.checkpoint(actor, mailbox)
            return BusinessArchivedPage(
                uidvalidity=checkpoint.uidvalidity,
                after_uid=checkpoint.after_uid,
                next_uid=checkpoint.after_uid,
                snapshot_end_uid=checkpoint.after_uid,
                messages=(),
            )

    lock = PostgresAdvisoryLock(
        integration_engine,
        derive_advisory_lock_key(actor.tenant_id, "controlled-race:" + mailbox),
    )
    assert await lock.acquire()
    try:
        driver = BusinessMailDriver(
            repo,
            actor,
            mailbox,
            NoNewMail(),
            classifier,
            lock,
            model="fixture",
            version="v1",
            stop=asyncio.Event(),
            limits=policy,
        )
        await driver.once()
        first = (await repo.list(actor, bucket="all", offset=0, limit=50)).items[0]
        assert first.classification_status == "pending" and first.category == "pending"
        assert provider.calls == 0
        assert (await repo.detail(actor, first.message_id)).classification_history == []
        async with sessions() as db:
            invocations = (
                await db.scalars(
                    select(ModelInvocationRow).where(
                        ModelInvocationRow.tenant_id == actor.tenant_id
                    )
                )
            ).all()
            assert len(invocations) == 1 and invocations[0].run_id == competitor.run_id
            refused = (
                await db.scalars(
                    select(ToolCallRow).where(
                        ToolCallRow.tenant_id == actor.tenant_id,
                        ToolCallRow.tool_id == "model.generate",
                        ToolCallRow.error_category == "rate_limited",
                    )
                )
            ).one()
            prior = (
                refused.tool_call_id,
                refused.idempotency_key,
                refused.status,
                refused.error_category,
            )
        if race == "concurrency":
            await competing.mark_dispatched(
                actor.tenant_id, reservation[0].invocation_id
            )
            await competing.finish(
                actor.tenant_id,
                reservation[0].invocation_id,
                ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
                "succeeded",
            )
        else:
            stamp[0] += timedelta(seconds=policy.window_seconds)
        assert await driver.once() == 1
        assert provider.calls == 1
        final = await repo.detail(actor, first.message_id)
        assert (
            final.category == "business" and final.classification_status == "classified"
        )
        assert len(final.classification_history) == 1
        assert await driver.once() == 0 and provider.calls == 1
        async with sessions() as db:
            old = await db.scalar(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == actor.tenant_id,
                    ToolCallRow.tool_call_id == prior[0],
                )
            )
            assert (
                old.tool_call_id,
                old.idempotency_key,
                old.status,
                old.error_category,
            ) == prior
            invocations = (
                await db.scalars(
                    select(ModelInvocationRow).where(
                        ModelInvocationRow.tenant_id == actor.tenant_id,
                        ModelInvocationRow.capability == "business_mail_triage",
                    )
                )
            ).all()
            assert len(invocations) == 1 and invocations[0].sequence == 1
            assert invocations[0].run_id == refused.run_id
    finally:
        await lock.close()


@pytest.mark.parametrize("state", ["reserved", "dispatched", "unknown", "rejected"])
async def test_existing_invocation_never_becomes_a_fresh_quota_attempt(
    integration_engine, state
):
    (
        sessions,
        actor,
        _,
        repo,
        _,
        claim,
        identity,
        classifier,
        provider,
    ) = await configured_model(integration_engine)
    usage = SqlModelUsageRepository(sessions)
    policy = classifier.generator._limits
    reservation = await usage.reserve(
        identity, "c" * 64, policy, datetime.now(UTC), model="fixture"
    )
    assert reservation.outcome == "reserved"
    unknown = ModelUsage(
        input_tokens=None, cached_input_tokens=None, output_tokens=None
    )
    if state in {"dispatched", "unknown"}:
        await usage.mark_dispatched(actor.tenant_id, reservation.invocation_id)
    if state in {"unknown", "rejected"}:
        await usage.finish(actor.tenant_id, reservation.invocation_id, unknown, state)
    assert await repo.defer_unattempted_quota(actor, claim) is False
    view = await usage.get(actor.tenant_id, reservation.invocation_id)
    assert view.state == state and provider.calls == 0
    with pytest.raises(ModelGenerationError):
        # 即使调用者误把quota传给原流程，也不能跨已有计量创建新身份或派发。
        await classifier.classify(identity, subject=claim.subject, body=claim.body_text)
    assert provider.calls == 0
