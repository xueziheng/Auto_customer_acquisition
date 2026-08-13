"""真实 PostgreSQL 下 Gmail 单封发送的崩溃与模糊结果恢复语义。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.gmail.client import GmailConnector
from connectors.gmail.transport import GmailNetworkError
from domains.outreach.schemas import (
    DeliveryCorrelationBinding,
    MessageAttemptState,
    MessageAttemptView,
    MessageSendPreflight,
)
from infra.db.session import create_engine_from
from infra.db.tables import ToolCallEventRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    MessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_send import DeliveryMaterial, EmailSendHandler
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
)
from tool_gateway.repository import ToolCallId, ToolCallRecord
from workflows.email_feedback.unsubscribe import UnsubscribeLink


@dataclass
class _Clock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance_past_lease(self) -> None:
        self.current += timedelta(seconds=6)


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "GMAIL_OAUTH_TOKEN_REF"
        return "controlled-oauth-value"


class _Transport:
    def __init__(
        self,
        *,
        searches: list[str | None | Exception],
        sends: list[str | Exception],
    ) -> None:
        self.searches = searches
        self.sends = sends
        self.search_calls = 0
        self.send_calls = 0

    async def search(self, **kwargs: object) -> str | None:
        del kwargs
        self.search_calls += 1
        outcome = self.searches.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def send(self, **kwargs: object) -> str:
        del kwargs
        self.send_calls += 1
        outcome = self.sends.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Materials:
    def __init__(self, preflight: MessageSendPreflight) -> None:
        self.preflight = preflight

    async def resolve(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> DeliveryMaterial:
        assert preflight == self.preflight
        return DeliveryMaterial(
            tenant_id=tenant_id,
            attempt_id=preflight.attempt_id,
            account_id=preflight.account_id,
            contact_point_id=preflight.contact_point_id,
            sending_identity_id=preflight.sending_identity_id,
            from_address="recovery-sender@example.test",
            recipient_address="recovery-recipient@example.test",
        )


class _Links:
    async def build(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> UnsubscribeLink:
        return UnsubscribeLink(
            tenant_id,
            preflight.attempt_id,
            preflight.contact_point_id,
            f"https://unsubscribe.example.test/{tenant_id}/{preflight.attempt_id}",
            "2026-v1",
            "unsubscribe-v1",
        )


class _Outreach:
    def __init__(self, preflight: MessageSendPreflight, now: datetime) -> None:
        self._preflight = preflight
        self._now = now

    async def bind_delivery_correlation(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        binding: DeliveryCorrelationBinding,
        *,
        actor: object,
    ) -> MessageAttemptView:
        del actor
        preflight = self._preflight
        assert (tenant_id, attempt_id) == (preflight.tenant_id, preflight.attempt_id)
        return MessageAttemptView(
            tenant_id=tenant_id,
            attempt_id=attempt_id,
            message_id=MessageId(new_id("msg")),
            campaign_id=preflight.campaign_id,
            enrollment_id=preflight.enrollment_id,
            campaign_version=preflight.campaign_version,
            step_number=preflight.step_number,
            sending_identity_id=preflight.sending_identity_id,
            idempotency_key=preflight.idempotency_key,
            state=MessageAttemptState.RESERVED,
            provider_ref=None,
            failure_category=None,
            created_at=self._now,
            updated_at=self._now,
            deterministic_message_id=binding.deterministic_message_id,
            idempotency_header=binding.idempotency_header,
        )


class _Stage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def check(self, ctx: object, state: object) -> None:
        del ctx, state
        self.calls += 1


class _SuppressionStage(_Stage):
    def __init__(self, preflight: MessageSendPreflight) -> None:
        super().__init__("suppression")
        self.preflight = preflight
        self.blocked = False

    async def check(self, ctx: object, state: object) -> CheckRejection | None:
        del ctx
        self.calls += 1
        if self.blocked:
            return CheckRejection(
                "suppression",
                "outreach:current_fact",
                "当前触达事实不允许发送",
            )
        state.preflight = self.preflight
        return None


class _RateStage(_Stage):
    def __init__(self) -> None:
        super().__init__("rate_limit")
        self.provider_ref: str | None = None
        self.fail_completion = False

    async def record_sent(
        self, ctx: object, state: object, provider_ref: str
    ) -> None:
        del ctx, state
        if self.fail_completion:
            raise RuntimeError("attempt-completion-marker")
        if self.provider_ref is not None and self.provider_ref != provider_ref:
            raise ValidationError("provider ref 冲突")
        self.provider_ref = provider_ref


@dataclass
class _Harness:
    tenant: TenantId
    clock: _Clock
    preflight: MessageSendPreflight
    context: ToolCallContext
    gateway: ToolGateway
    handler: EmailSendHandler
    transport: _Transport
    suppression: _SuppressionStage
    rate: _RateStage
    factory: async_sessionmaker


def _manifest() -> ToolManifest:
    return ToolManifest(
        tool_id="email.send",
        version="v1",
        description="发送当前已批准 Campaign 的单封邮件",
        risk_level=RiskLevel.HIGH,
        cost_class=CostClass.LOW,
        requires_approval=False,
        idempotency=IdempotencyRequirement.REQUIRED,
        required_permissions=("outreach:message_send",),
        checks=(
            "tenant",
            "permission",
            "suppression",
            "approval",
            "idempotency",
            "rate_limit",
        ),
    )


async def _harness(
    database_url: str,
    *,
    searches: list[str | None | Exception],
    sends: list[str | Exception],
    session_class: type[AsyncSession] = AsyncSession,
) -> tuple[object, _Harness]:
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=session_class
    )
    tenant = TenantId(new_id("tn"))
    clock = _Clock(datetime(2026, 8, 13, 6, 0, tzinfo=UTC))
    preflight = MessageSendPreflight(
        tenant_id=tenant,
        attempt_id=MessageAttemptId(new_id("mat")),
        campaign_id=CampaignId(new_id("cmp")),
        enrollment_id=EnrollmentId(new_id("enr")),
        account_id=ProspectAccountId(new_id("acc")),
        contact_point_id=ContactPointId(new_id("cp")),
        sending_identity_id=SendingIdentityId(new_id("sid")),
        campaign_version=1,
        step_number=1,
        idempotency_key=IdempotencyKey("recovery-message-key"),
    )
    context = ToolCallContext(
        tenant_id=tenant,
        user_id=UserId(new_id("usr")),
        tool_id="email.send",
        params={
            "attempt_id": str(preflight.attempt_id),
            "subject": "recovery-subject-marker",
            "body": "recovery-body-marker",
        },
        idempotency_key=preflight.idempotency_key,
        campaign_ref=str(preflight.campaign_id),
    )
    transport = _Transport(searches=searches, sends=sends)
    gmail = GmailConnector(transport)
    await gmail.configure(_Secrets())
    handler = EmailSendHandler(
        gmail,
        _Materials(preflight),
        _Links(),
        HmacFingerprintProvider("recovery-v1", b"r" * 32),
        outreach=_Outreach(preflight, clock.now()),  # type: ignore[arg-type]
        outreach_actor_factory=lambda _attempt: object(),  # type: ignore[arg-type,return-value]
        route_id="feedback-route-v1",
    )
    registry = ToolRegistry()
    registry.register(_manifest(), handler)
    suppression = _SuppressionStage(preflight)
    rate = _RateStage()
    gateway = ToolGateway(
        registry,
        {
            "tenant": _Stage("tenant"),
            "permission": _Stage("permission"),
            "suppression": suppression,
            "approval": _Stage("approval"),
            "idempotency": _Stage("idempotency"),
            "rate_limit": rate,
        },
        lambda requested: SqlAlchemyToolGatewayUnitOfWork(
            factory, requested, now=clock.now
        ),
        lease_duration=timedelta(seconds=5),
        lease_owner="recovery-worker",
        now=clock.now,
        id_factory=new_id,
    )
    return engine, _Harness(
        tenant,
        clock,
        preflight,
        context,
        gateway,
        handler,
        transport,
        suppression,
        rate,
        factory,
    )


def _received(harness: _Harness, call_id: ToolCallId) -> ToolCallRecord:
    now = harness.clock.now()
    return ToolCallRecord(
        tenant_id=harness.tenant,
        tool_call_id=call_id,
        tool_id="email.send",
        tool_version="v1",
        risk_level="high",
        cost_class="low",
        idempotency_key=None,
        request_fingerprint=None,
        fingerprint_version=None,
        status=ToolCallStatus.RECEIVED,
        duplicate_of=None,
        lease_owner=None,
        lease_expires_at=None,
        attempt_count=0,
        run_id=None,
        user_id=harness.context.user_id,
        campaign_id=str(harness.preflight.campaign_id),
        message_attempt_id=str(harness.preflight.attempt_id),
        provider_ref=None,
        error_category=None,
        retry_after_at=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


async def _seed_crashed_claim(
    harness: _Harness, *, executing: bool
) -> ToolCallId:
    call_id = ToolCallId(new_id("tcl"))
    prepared = await harness.handler.prepare(harness.context, harness.preflight)
    async with SqlAlchemyToolGatewayUnitOfWork(
        harness.factory, harness.tenant, now=harness.clock.now
    ) as uow:
        await uow.calls.create_received(_received(harness, call_id))
        claimed = await uow.calls.claim(
            harness.tenant,
            call_id,
            tool_id="email.send",
            idempotency_key=harness.preflight.idempotency_key,
            request_fingerprint=prepared.request_fingerprint,
            fingerprint_version=prepared.fingerprint_version,
            lease_owner="crashed-worker",
            lease_expires_at=harness.clock.now() + timedelta(seconds=1),
        )
        if executing:
            await uow.calls.mark_executing(harness.tenant, call_id)
        assert claimed.canonical.tool_call_id == call_id
    harness.clock.advance_past_lease()
    return call_id


async def test_expired_pre_execution_claim_reruns_checks_and_sends_once(
    db_url: str,
) -> None:
    engine, harness = await _harness(
        db_url,
        searches=[None],
        sends=["gmail_ref_recovered_claim"],
    )
    try:
        call_id = await _seed_crashed_claim(harness, executing=False)

        result = await harness.gateway.invoke(harness.context)

        assert result.status is ToolCallStatus.SUCCEEDED
        assert result.tool_call_id == call_id
        assert harness.suppression.calls == 1
        assert harness.transport.search_calls == 1
        assert harness.transport.send_calls == 1
    finally:
        await engine.dispose()


async def test_committed_executing_crash_requires_reconciliation_without_gmail(
    db_url: str,
) -> None:
    engine, harness = await _harness(
        db_url,
        searches=[],
        sends=[],
    )
    try:
        call_id = await _seed_crashed_claim(harness, executing=True)

        result = await harness.gateway.invoke(harness.context)

        assert result.status is ToolCallStatus.FAILED_TRANSIENT
        assert result.tool_call_id == call_id
        assert result.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert harness.transport.search_calls == 0
        assert harness.transport.send_calls == 0
    finally:
        await engine.dispose()


async def test_ambiguous_retry_search_hit_completes_without_resend(
    db_url: str,
) -> None:
    engine, harness = await _harness(
        db_url,
        searches=[None, "gmail_ref_reconciled"],
        sends=[GmailNetworkError(may_have_written=True)],
    )
    try:
        first = await harness.gateway.invoke(harness.context)
        harness.clock.advance_past_lease()
        recovered = await harness.gateway.invoke(harness.context)

        assert first.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert recovered.status is ToolCallStatus.SUCCEEDED
        assert recovered.tool_call_id == first.tool_call_id
        assert recovered.output == {
            "provider_ref": "gmail_ref_reconciled",
            "already_existed": True,
        }
        assert harness.transport.search_calls == 2
        assert harness.transport.send_calls == 1
    finally:
        await engine.dispose()


async def test_ambiguous_retry_search_miss_never_resends(
    db_url: str,
) -> None:
    engine, harness = await _harness(
        db_url,
        searches=[None, None],
        sends=[
            GmailNetworkError(may_have_written=True),
            "gmail_ref_forbidden_resend",
        ],
    )
    try:
        first = await harness.gateway.invoke(harness.context)
        harness.clock.advance_past_lease()
        recovered = await harness.gateway.invoke(harness.context)

        assert first.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert recovered.status is ToolCallStatus.FAILED_TRANSIENT
        assert recovered.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert harness.transport.search_calls == 2
        assert harness.transport.send_calls == 1
        assert harness.rate.provider_ref is None
    finally:
        await engine.dispose()


async def test_definitely_not_sent_retry_reruns_current_facts_and_can_be_suppressed(
    db_url: str,
) -> None:
    engine, harness = await _harness(
        db_url,
        searches=[None],
        sends=[GmailNetworkError(may_have_written=False)],
    )
    try:
        first = await harness.gateway.invoke(harness.context)
        harness.clock.advance_past_lease()
        harness.suppression.blocked = True
        blocked = await harness.gateway.invoke(harness.context)

        assert first.error_category is ToolErrorCategory.PROVIDER_TRANSIENT
        assert blocked.status is ToolCallStatus.REJECTED
        assert blocked.error_category is ToolErrorCategory.SUPPRESSED
        assert harness.suppression.calls == 2
        assert harness.transport.search_calls == 1
        assert harness.transport.send_calls == 1
    finally:
        await engine.dispose()


async def test_executing_event_commit_failure_never_calls_gmail_and_is_retryable(
    db_url: str,
) -> None:
    failed = False

    class FailExecutingEventCommitOnce(AsyncSession):
        async def commit(self) -> None:
            nonlocal failed
            has_executing_event = any(
                isinstance(row, ToolCallEventRow)
                and row.stage == "ledger"
                and row.outcome == "executing"
                for row in self.new
            )
            if not failed and has_executing_event:
                failed = True
                raise RuntimeError("executing-event-commit-marker")
            await super().commit()

    engine, harness = await _harness(
        db_url,
        searches=[None],
        sends=["gmail_ref_after_retry"],
        session_class=FailExecutingEventCommitOnce,
    )
    try:
        first = await harness.gateway.invoke(harness.context)

        assert first.status is ToolCallStatus.FAILED_TRANSIENT
        assert first.error_category is ToolErrorCategory.PROVIDER_TRANSIENT
        assert harness.transport.search_calls == 0
        assert harness.transport.send_calls == 0
        harness.clock.advance_past_lease()
        retried = await harness.gateway.invoke(harness.context)
        assert retried.status is ToolCallStatus.SUCCEEDED
        assert harness.transport.search_calls == 1
        assert harness.transport.send_calls == 1
        async with harness.factory() as session:
            events = (
                await session.execute(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == harness.tenant,
                        ToolCallEventRow.tool_call_id == first.tool_call_id,
                    )
                )
            ).scalars().all()
        assert sum(
            event.stage == "ledger" and event.outcome == "executing"
            for event in events
        ) == 1
    finally:
        await engine.dispose()


async def test_attempt_completion_failure_recovers_by_search_without_resend(
    db_url: str,
) -> None:
    engine, harness = await _harness(
        db_url,
        searches=[None, "gmail_ref_attempt_commit"],
        sends=["gmail_ref_attempt_commit"],
    )
    harness.rate.fail_completion = True
    try:
        first = await harness.gateway.invoke(harness.context)
        assert first.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        harness.rate.fail_completion = False
        harness.clock.advance_past_lease()

        recovered = await harness.gateway.invoke(harness.context)

        assert recovered.status is ToolCallStatus.SUCCEEDED
        assert harness.rate.provider_ref == "gmail_ref_attempt_commit"
        assert harness.transport.search_calls == 2
        assert harness.transport.send_calls == 1
    finally:
        await engine.dispose()


async def test_canonical_completion_failure_and_provider_mismatch_never_overwrite(
    db_url: str,
) -> None:
    failed = False

    class FailSucceededLedgerCommitOnce(AsyncSession):
        async def commit(self) -> None:
            nonlocal failed
            has_succeeded_call = any(
                isinstance(row, ToolCallRow) and row.status == "succeeded"
                for row in self.dirty
            )
            if not failed and has_succeeded_call:
                failed = True
                raise RuntimeError("canonical-completion-commit-marker")
            await super().commit()

    engine, harness = await _harness(
        db_url,
        searches=[None, "gmail_ref_mismatch"],
        sends=["gmail_ref_original"],
        session_class=FailSucceededLedgerCommitOnce,
    )
    try:
        first = await harness.gateway.invoke(harness.context)
        assert first.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert harness.rate.provider_ref == "gmail_ref_original"
        harness.clock.advance_past_lease()

        mismatch = await harness.gateway.invoke(harness.context)

        assert mismatch.status is ToolCallStatus.FAILED_TRANSIENT
        assert mismatch.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert harness.rate.provider_ref == "gmail_ref_original"
        assert harness.transport.search_calls == 2
        assert harness.transport.send_calls == 1
        async with harness.factory() as session:
            canonical = (
                await session.execute(
                    select(ToolCallRow).where(
                        ToolCallRow.tenant_id == harness.tenant,
                        ToolCallRow.tool_call_id == first.tool_call_id,
                    )
                )
            ).scalar_one()
            events = (
                await session.execute(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == harness.tenant,
                        ToolCallEventRow.tool_call_id == first.tool_call_id,
                    )
                )
            ).scalars().all()
        assert canonical.status == "failed_transient"
        assert canonical.provider_ref is None
        assert canonical.error_category == "reconciliation_required"
        persisted = repr((canonical.__dict__, [event.__dict__ for event in events]))
        for raw_value in (
            "recovery-sender@example.test",
            "recovery-recipient@example.test",
            "recovery-subject-marker",
            "recovery-body-marker",
            "controlled-oauth-value",
            "https://unsubscribe.example.test",
            "canonical-completion-commit-marker",
        ):
            assert raw_value not in persisted
    finally:
        await engine.dispose()
