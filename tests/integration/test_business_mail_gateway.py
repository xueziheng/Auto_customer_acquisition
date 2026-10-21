"""真实Postgres授权/工具账本/模型额度，仅QQ与模型/对象传输为受控外部端口。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from agent_runtime.business_mail import BusinessMailClassifier
from apps.email_feedback_worker.business_mail import (
    BusinessMailModelAuthority,
    _ActorAuthority,
)
from artifact_store.service_impl import RawArtifactStoreImpl
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.tables import EmployeeRow, ModelInvocationRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.email_inbound_artifacts import InboundRawArtifactArchiver
from shared.business_mail import BusinessFetchPage
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelResponse,
    ModelUsage,
)
from tests.integration.test_business_mail import setup_mail
from tests.integration.test_model_usage import limits
from tests.unit.test_business_mail_imap import RAW
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.business_mail import GatewayBusinessMailReader
from tool_gateway.handlers.model_generate import GatewayModelGenerator


class BlobTransport:
    """只替换对象服务传输；metadata与事务保持真实Postgres。"""

    def __init__(self):
        self.objects = {}

    async def put(self, key, content):
        self.objects[key] = content

    async def get(self, key):
        return self.objects[key]

    async def delete(self, key):
        del self.objects[key]

    async def get_bounded(self, key, *, maximum_bytes):
        content = self.objects[key]
        assert len(content) <= maximum_bytes
        return content


async def test_fetch_archives_real_source_before_visible_and_ledger_has_no_content(
    integration_engine,
):
    from connectors.qq_mail.business_inbound import parse_business_mime

    sessions, actor, _, repo, mailbox, _ = await setup_mail(integration_engine)
    blobs = BlobTransport()
    store = RawArtifactStoreImpl(
        lambda t: SqlAlchemyArtifactUnitOfWork(sessions, t),
        blobs,
        4 * 1024 * 1024,
        lambda: datetime.now(UTC),
        __import__("shared.schemas.identifiers", fromlist=["new_id"]).new_id,
        bounded_transport=blobs,
    )
    page = BusinessFetchPage(
        uidvalidity=7,
        after_uid=0,
        next_uid=1,
        snapshot_end_uid=1,
        messages=(parse_business_mime(RAW, uid=1, uidvalidity=7),),
    )

    class Reader:
        async def fetch(self, **kwargs):
            return page

    async def authorize():
        await repo.authorize(actor, mailbox)

    fingerprints = HmacFingerprintProvider("v1", b"controlled-fingerprint-key-32-bytes")
    reader = GatewayBusinessMailReader(
        tenant=actor.tenant_id,
        user=actor.user_id,
        mailbox=mailbox,
        reader=Reader,
        archiver=InboundRawArtifactArchiver(store, store),
        authorize=authorize,
        fingerprints=fingerprints,
        ledger_factory=lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t),
        lease_owner="controlled-business",
    )
    checkpoint = await repo.checkpoint(actor, mailbox)
    archived = await reader.fetch(uidvalidity=None, after_uid=0)
    assert (await repo.list(actor, bucket="all", offset=0, limit=50)).items == []
    await repo.apply_page(actor, checkpoint, archived)
    detail = await repo.detail(
        actor,
        (await repo.list(actor, bucket="pending", offset=0, limit=50))
        .items[0]
        .message_id,
    )
    meta, actual = await store.get_bounded(
        actor.tenant_id, detail.source.artifact_id, maximum_bytes=4 * 1024 * 1024
    )
    assert actual == RAW and meta.content_hash == detail.source.mime_sha256
    async with sessions() as db:
        calls = (
            await db.scalars(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == actor.tenant_id,
                    ToolCallRow.tool_id == "email.business.fetch",
                )
            )
        ).all()
        assert len(calls) == 1
        projection = str(
            {c.name: getattr(calls[0], c.name) for c in ToolCallRow.__table__.columns}
        )
        assert "hinges" not in projection and "sales@example.com" not in projection


class Provider:
    def __init__(self, after=None):
        self.after = after
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        if self.after:
            await self.after()
        return ModelResponse(
            text=json.dumps(
                {
                    "category": "business",
                    "reason": "业务询价",
                    "evidence_quote": "hinges",
                }
            ),
            model=request.model,
            usage=ModelUsage(input_tokens=10, cached_input_tokens=0, output_tokens=20),
        )

    async def aclose(self):
        pass


async def configured_model(
    engine,
    provider=None,
    policy_override=None,
    *,
    claim_message=True,
    policy_changes=None,
):
    sessions, actor, current, repo, mailbox, page = await setup_mail(engine)
    await repo.apply_page(actor, await repo.checkpoint(actor, mailbox), page)
    fingerprint = HmacFingerprintProvider("v1", b"controlled-fingerprint-key-32-bytes")
    policy = limits(
        **{
            "tenant_calls": 3,
            "employee_calls": 3,
            "max_input_bytes": 32768,
            "max_output_tokens": 512,
            "timeout_seconds": 60,
            **(policy_changes or {}),
        }
    )
    stamp = datetime.now(UTC)
    configurations = SqlModelConfigurationRepository(
        sessions, fingerprint, deployment_verification=(actor.tenant_id, "v1", stamp)
    )
    await configurations.initialize(actor.tenant_id, "v1", "fixture", policy, True)
    await configurations.register_process(actor.tenant_id, "api", "v1", "api-fixture")
    await configurations.register_process(
        actor.tenant_id, "scheduler", "v1", "scheduler-fixture"
    )
    configuration = ModelConfigurationServiceImpl(
        configurations,
        _ActorAuthority(repo, actor, mailbox),
        now=lambda: datetime.now(UTC),
    )
    authority = BusinessMailModelAuthority(
        repo,
        actor,
        configuration,
        "fixture",
        "v1",
        configuration_repository=configurations,
        limits=policy_override or policy,
    )
    provider = provider or Provider()
    generator = GatewayModelGenerator(
        authority=authority,
        usage=SqlModelUsageRepository(sessions),
        provider_factory=lambda: provider,
        limits=policy_override or policy,
        model="fixture",
        configuration_version="v1",
        ledger_factory=lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t),
        fingerprints=fingerprint,
        lease_owner="business-fixture",
        lease_duration=timedelta(seconds=90),
    )
    claim = (
        await repo.claim_next(actor, mailbox, model="fixture", version="v1")
        if claim_message
        else None
    )
    identity = (
        InvocationIdentity(
            **actor.model_dump(),
            run_id=claim.run_id,
            capability="business_mail_triage",
            configuration_version="v1",
            sequence=0,
        )
        if claim is not None
        else None
    )
    return (
        sessions,
        actor,
        current,
        repo,
        mailbox,
        claim,
        identity,
        BusinessMailClassifier(generator, model="fixture", max_output_tokens=512),
        provider,
    )


async def test_model_uses_existing_configuration_quota_and_deduplicates(
    integration_engine,
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
    result = await classifier.classify(
        identity, subject=claim.subject, body=claim.body_text
    )
    await repo.finish(actor, claim, result=result, failure=None)
    detail = await repo.detail(actor, claim.message_id)
    assert (
        detail.category == "business"
        and detail.classification_history[0].origin == "model"
    )
    with pytest.raises(ModelGenerationError):
        await classifier.classify(identity, subject=claim.subject, body=claim.body_text)
    assert provider.calls == 1
    async with sessions() as db:
        invocations = (
            await db.scalars(
                select(ModelInvocationRow).where(
                    ModelInvocationRow.tenant_id == actor.tenant_id
                )
            )
        ).all()
    assert len(invocations) == 1 and invocations[0].capability == "business_mail_triage"
    assert invocations[0].state == "succeeded"


async def test_revocation_before_or_during_model_does_not_apply_result(
    integration_engine,
):
    (
        sessions,
        actor,
        _current,
        repo,
        _,
        claim,
        identity,
        classifier,
        provider,
    ) = await configured_model(integration_engine)

    async def deactivate():
        async with sessions.begin() as db:
            await db.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == actor.tenant_id,
                    EmployeeRow.employee_id == actor.employee_id,
                )
                .values(is_active=False)
            )

    provider.after = deactivate
    with pytest.raises(ModelGenerationError):
        await classifier.classify(identity, subject=claim.subject, body=claim.body_text)
    assert provider.calls == 1
    from shared.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        await repo.detail(actor, claim.message_id)


async def test_correction_before_dispatch_blocks_model(integration_engine):
    from domains.conversations.business_mail import BusinessMailCorrection

    (
        _,
        actor,
        _,
        repo,
        _,
        claim,
        identity,
        classifier,
        provider,
    ) = await configured_model(integration_engine)
    await repo.correct(
        actor,
        claim.message_id,
        BusinessMailCorrection(
            expected_revision=0, category="business", note="人工已确认归类"
        ),
    )
    with pytest.raises(ModelGenerationError):
        await classifier.classify(identity, subject=claim.subject, body=claim.body_text)
    assert provider.calls == 0


async def test_human_correction_during_provider_preserves_original_model_history(
    integration_engine,
):
    from domains.conversations.business_mail import BusinessMailCorrection

    (
        _,
        actor,
        _,
        repo,
        _,
        claim,
        identity,
        classifier,
        provider,
    ) = await configured_model(integration_engine)

    async def correct():
        await repo.correct(
            actor,
            claim.message_id,
            BusinessMailCorrection(
                expected_revision=0, category="other", note="人工核实其他邮件"
            ),
        )

    provider.after = correct
    result = await classifier.classify(
        identity, subject=claim.subject, body=claim.body_text
    )
    await repo.finish(actor, claim, result=result, failure=None)
    detail = await repo.detail(actor, claim.message_id)
    assert detail.category == "other"
    assert [item.origin for item in detail.classification_history] == ["human", "model"]
    assert detail.classification_history[1].category == "business"


async def test_local_profile_cannot_expand_persisted_model_limits(integration_engine):
    expanded = limits(
        tenant_calls=300,
        employee_calls=300,
        max_input_bytes=32768,
        max_output_tokens=512,
        timeout_seconds=60,
    )
    (
        sessions,
        actor,
        _,
        _,
        _,
        claim,
        identity,
        classifier,
        provider,
    ) = await configured_model(integration_engine, policy_override=expanded)
    with pytest.raises(ModelGenerationError) as caught:
        await classifier.classify(identity, subject=claim.subject, body=claim.body_text)
    assert caught.value.code == "configuration" and provider.calls == 0
    async with sessions() as db:
        assert (
            await db.scalars(
                select(ModelInvocationRow).where(
                    ModelInvocationRow.tenant_id == actor.tenant_id
                )
            )
        ).all() == []
