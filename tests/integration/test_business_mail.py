"""真实Postgres验证来信先可见、整页幂等、CAS和当前owner撤权；外部端口受控。"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.conversations.business_mail import (
    BusinessMailActor,
    BusinessMailConflict,
    BusinessMailCorrection,
)
from infra.db.tables import EmployeeRow, RawArtifactRow
from shared.business_mail import ArchivedBusinessMail, BusinessArchivedPage
from shared.errors import PermissionDenied
from shared.schemas.identifiers import new_id

NOW = datetime(2026, 10, 21, tzinfo=UTC)


class CurrentBinding:
    def __init__(self):
        self.operation = "aco-current"

    async def check(self, actor, address, operation):
        if operation != self.operation:
            raise PermissionDenied("当前邮箱绑定已改变")


async def setup_mail(engine):
    from infra.db.business_mail import SqlBusinessMailRepository

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = BusinessMailActor(
        tenant_id=new_id("tn"), employee_id=new_id("emp"), user_id=new_id("usr")
    )
    current = CurrentBinding()
    repo = SqlBusinessMailRepository(sessions, current)
    async with sessions.begin() as session:
        session.add(
            EmployeeRow(
                tenant_id=actor.tenant_id,
                employee_id=actor.employee_id,
                user_id=actor.user_id,
                name="当前负责人",
                role="sales",
                is_active=True,
            )
        )
    mailbox = await repo.register(
        actor, address="1249583590@qq.com", operation="aco-current"
    )
    artifact = new_id("art")
    digest = hashlib.sha256(b"controlled MIME").hexdigest()
    async with sessions.begin() as session:
        session.add(
            RawArtifactRow(
                tenant_id=actor.tenant_id,
                artifact_id=artifact,
                kind="email_raw",
                content_hash=digest,
                mime_type="message/rfc822",
                size_bytes=15,
                object_key=f"raw/{actor.tenant_id}/{artifact}",
                uploaded_by=None,
                uploaded_at=NOW,
            )
        )
    message = ArchivedBusinessMail(
        uidvalidity=7,
        uid=1,
        sender="sales@example.com",
        subject="Inquiry",
        body_text="Please quote hinges.",
        message_header="<first@example.com>",
        received_at=NOW,
        artifact_id=artifact,
        mime_sha256=digest,
        source_sha256=digest,
        content_sha256=hashlib.sha256(b"content").hexdigest(),
    )
    page = BusinessArchivedPage(
        uidvalidity=7, after_uid=0, next_uid=1, messages=(message,)
    )
    return sessions, actor, current, repo, mailbox, page


async def test_first_mail_visible_before_model_and_failure_keeps_original(
    integration_engine,
):
    _sessions, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    checkpoint = await repo.checkpoint(actor, mailbox)
    await repo.apply_page(actor, checkpoint, page)
    visible = await repo.list(actor, bucket="all", offset=0, limit=50)
    assert len(visible.items) == 1 and visible.items[0].category == "pending"
    assert visible.items[0].contact_status == "unconfirmed"
    claim = await repo.claim_next(
        actor, mailbox, model="test-model", version="version-1"
    )
    assert claim is not None
    await repo.finish(
        actor,
        claim,
        result=None,
        failure="quota",
        model_blocked_until=datetime.now(UTC) + timedelta(days=1),
    )
    detail = await repo.detail(actor, visible.items[0].message_id)
    assert detail.body_text == "Please quote hinges."
    assert detail.failure_code == "quota" and detail.category == "review"
    assert detail.classification_status == "failed"
    assert (
        await repo.claim_next(actor, mailbox, model="test-model", version="version-1")
        is None
    )
    assert detail.source.artifact_id == page.messages[0].artifact_id
    assert detail.classification_history[0].origin == "system"


async def test_duplicate_uid_and_reset_deduplicate_without_reclassification(
    integration_engine,
):
    _sessions, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    checkpoint = await repo.checkpoint(actor, mailbox)
    await repo.apply_page(actor, checkpoint, page)
    with pytest.raises(BusinessMailConflict):
        await repo.apply_page(actor, checkpoint, page)
    checkpoint = await repo.checkpoint(actor, mailbox)
    reset = page.model_copy(
        update={
            "uidvalidity": 8,
            "messages": (
                page.messages[0].model_copy(update={"uidvalidity": 8, "uid": 1}),
            ),
        }
    )
    await repo.apply_page(actor, checkpoint, reset)
    assert len((await repo.list(actor, bucket="all", offset=0, limit=50)).items) == 1


async def test_correction_preserves_original_and_enforces_compare_and_swap(
    integration_engine,
):
    _, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    claim = await repo.claim_next(
        actor, mailbox, model="test-model", version="version-1"
    )
    from domains.conversations.business_mail import BusinessMailResult

    await repo.finish(
        actor,
        claim,
        result=BusinessMailResult(
            category="other", reason="测试原判", evidence_quote="hinges"
        ),
        failure=None,
    )
    original = await repo.detail(actor, claim.message_id)
    corrected = await repo.correct(
        actor,
        claim.message_id,
        BusinessMailCorrection(
            expected_revision=original.revision, category="business", note="业务询价"
        ),
    )
    assert [h.origin for h in corrected.classification_history] == ["model", "human"]
    with pytest.raises(BusinessMailConflict):
        await repo.correct(
            actor,
            claim.message_id,
            BusinessMailCorrection(
                expected_revision=original.revision, category="other", note="过期页面"
            ),
        )


async def test_inactive_owner_rotation_and_other_tenant_cannot_read_or_apply(
    integration_engine,
):
    sessions, actor, current, repo, mailbox, page = await setup_mail(integration_engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    message = (await repo.list(actor, bucket="all", offset=0, limit=50)).items[0]
    for changed in (
        actor.model_copy(update={"tenant_id": new_id("tn")}),
        actor.model_copy(update={"employee_id": new_id("emp")}),
    ):
        with pytest.raises(PermissionDenied):
            await repo.detail(changed, message.message_id)
    current.operation = "rotated"
    with pytest.raises(PermissionDenied):
        await repo.detail(actor, message.message_id)
    current.operation = "aco-current"
    async with sessions.begin() as session:
        await session.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == actor.tenant_id,
                EmployeeRow.employee_id == actor.employee_id,
            )
            .values(is_active=False)
        )
    with pytest.raises(PermissionDenied):
        await repo.list(actor, bucket="all", offset=0, limit=50)


async def test_two_workers_claim_once_and_restart_never_replays_unknown(
    integration_engine,
):
    import asyncio

    _, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    claims = await asyncio.gather(
        *(
            repo.claim_next(actor, mailbox, model="test-model", version="v1")
            for _ in range(2)
        )
    )
    assert sum(c is not None for c in claims) == 1
    assert await repo.recover_claims(actor, mailbox) == 1
    assert (
        await repo.claim_next(actor, mailbox, model="test-model", version="v1") is None
    )
    detail = await repo.detail(
        actor, next(c for c in claims if c is not None).message_id
    )
    assert detail.failure_code == "unknown" and detail.category == "review"


async def test_whole_page_rollback_and_missing_artifact_do_not_advance_cursor(
    integration_engine,
):
    _, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    checkpoint = await repo.checkpoint(actor, mailbox)
    bad = page.messages[0].model_copy(
        update={"uid": 2, "artifact_id": new_id("art"), "content_sha256": "b" * 64}
    )
    from shared.errors import ValidationError

    with pytest.raises(ValidationError):
        await repo.apply_page(
            actor,
            checkpoint,
            page.model_copy(
                update={"next_uid": 2, "messages": (page.messages[0], bad)}
            ),
        )
    assert (await repo.checkpoint(actor, mailbox)).revision == checkpoint.revision
    assert (await repo.list(actor, bucket="all", offset=0, limit=50)).items == []


async def test_headers_only_oversize_is_visible_with_no_full_mime_claim(
    integration_engine,
):
    _, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    item = page.messages[0].model_copy(
        update={
            "source_kind": "headers_only",
            "mime_sha256": None,
            "body_text": "",
            "parse_failure": "oversize",
        }
    )
    await repo.apply_page(
        actor,
        await repo.checkpoint(actor, mailbox),
        page.model_copy(update={"messages": (item,)}),
    )
    detail = await repo.detail(
        actor,
        (await repo.list(actor, bucket="review", offset=0, limit=50))
        .items[0]
        .message_id,
    )
    assert (
        detail.source.source_kind == "headers_only"
        and detail.source.mime_sha256 is None
    )
    assert detail.source.source_sha256 == item.source_sha256
    assert detail.failure_code == "oversize"
    assert await repo.claim_next(actor, mailbox, model="fixture", version="v1") is None


async def test_owner_correction_during_model_wins_and_original_is_retained(
    integration_engine,
):
    _, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    claim = await repo.claim_next(actor, mailbox, model="test-model", version="v1")
    await repo.correct(
        actor,
        claim.message_id,
        BusinessMailCorrection(
            expected_revision=0, category="business", note="人工识别业务询价"
        ),
    )
    from domains.conversations.business_mail import BusinessMailResult

    await repo.finish(
        actor,
        claim,
        result=BusinessMailResult(
            category="other", reason="模型原判", evidence_quote="hinges"
        ),
        failure=None,
    )
    detail = await repo.detail(actor, claim.message_id)
    assert detail.category == "business"
    assert [h.origin for h in detail.classification_history] == ["human", "model"]


async def test_quota_preview_preserves_pending_and_distinguishes_concurrency(
    integration_engine,
):
    from infra.db.model_usage import SqlModelUsageRepository
    from shared.schemas.model_invocation import InvocationIdentity, ModelUsage
    from tests.integration.test_model_usage import limits

    sessions, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    usage = SqlModelUsageRepository(sessions)
    config = limits(tenant_calls=3, employee_calls=3)
    identity = InvocationIdentity(
        **actor.model_dump(),
        run_id=new_id("run"),
        capability="product_help",
        configuration_version="v1",
        sequence=0,
    )
    reserved = await usage.reserve(
        identity, "a" * 64, config, datetime.now(UTC), model="fixture"
    )
    state, until = await repo.model_availability(actor, mailbox, config)
    assert state == "concurrency" and until is None
    assert (await repo.list(actor, bucket="pending", offset=0, limit=50)).items[
        0
    ].revision == 0
    await usage.mark_dispatched(actor.tenant_id, reserved.invocation_id)
    await usage.finish(
        actor.tenant_id,
        reserved.invocation_id,
        ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
        "succeeded",
    )
    assert (await repo.model_availability(actor, mailbox, config))[0] == "ready"
    state, until = await repo.model_availability(actor, mailbox, limits())
    assert state == "window" and until is not None
    assert await repo.claim_next(actor, mailbox, model="fixture", version="v1") is None
    assert (await repo.list(actor, bucket="pending", offset=0, limit=50)).items[
        0
    ].classification_status == "pending"


@pytest.mark.parametrize("updated_counts", [(None, None), (3, 3)])
async def test_changed_limits_clear_stale_pause_without_rewriting_usage(integration_engine, updated_counts):
    from sqlalchemy import func, select

    from infra.db.model_usage import SqlModelUsageRepository
    from infra.db.tables import ModelInvocationRow
    from shared.schemas.model_invocation import InvocationIdentity, ModelUsage
    from tests.integration.test_model_usage import limits

    sessions, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    usage = SqlModelUsageRepository(sessions)
    identity = InvocationIdentity(**actor.model_dump(), run_id=new_id("run"),
                                  capability="product_help", configuration_version="v1", sequence=0)
    reserved = await usage.reserve(identity, "a" * 64, limits(), datetime.now(UTC), model="fixture")
    await usage.mark_dispatched(actor.tenant_id, reserved.invocation_id)
    await usage.finish(actor.tenant_id, reserved.invocation_id,
                       ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1), "succeeded")
    assert (await repo.model_availability(actor, mailbox, limits()))[0] == "window"
    assert await repo.claim_next(actor, mailbox, model="fixture", version="v1") is None
    updated = limits(tenant_calls=updated_counts[0], employee_calls=updated_counts[1])
    assert await repo.model_availability(actor, mailbox, updated) == ("ready", None)
    assert await repo.claim_next(actor, mailbox, model="fixture", version="v2") is not None
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ModelInvocationRow).where(ModelInvocationRow.tenant_id == actor.tenant_id)) == 1
    assert (await usage.get(actor.tenant_id, reserved.invocation_id)).state == "succeeded"


async def test_immutable_original_and_classification_history_at_database_boundary(
    integration_engine,
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    sessions, actor, _, repo, mailbox, page = await setup_mail(integration_engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    message = (await repo.list(actor, bucket="all", offset=0, limit=50)).items[0]
    with pytest.raises(DBAPIError):
        async with sessions.begin() as db:
            await db.execute(
                text(
                    "UPDATE business_mail_messages SET body_text=:body WHERE tenant_id=:tenant AND message_id=:message"
                ),
                {
                    "body": "changed",
                    "tenant": actor.tenant_id,
                    "message": message.message_id,
                },
            )
    await repo.correct(
        actor,
        message.message_id,
        BusinessMailCorrection(
            expected_revision=0, category="business", note="人工归类"
        ),
    )
    with pytest.raises(DBAPIError):
        async with sessions.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM business_mail_classifications WHERE tenant_id=:tenant AND message_id=:message"
                ),
                {"tenant": actor.tenant_id, "message": message.message_id},
            )
    assert (
        await repo.detail(actor, message.message_id)
    ).body_text == "Please quote hinges."
