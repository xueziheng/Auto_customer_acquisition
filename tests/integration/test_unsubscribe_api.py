"""One-click 匿名 API 的真实 PostgreSQL 并发与原子副作用。"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections import Counter
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.composition.runtime import build_phase1_dependencies
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.api.runtime_config import Phase1RuntimeSettings
from domains.outreach.schemas import MessageSendPreflight
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.session import create_engine_from
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

NOW = datetime(2026, 8, 13, 14, 0, tzinfo=UTC)


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        values = {
            "UNSUBSCRIBE_HMAC_CURRENT": "u" * 32,
            "TOOL_FINGERPRINT_KEY": "f" * 32,
        }
        return values[secret_ref]


def _settings(tenant: TenantId) -> Phase1RuntimeSettings:
    return Phase1RuntimeSettings.from_environ(
        {
            "DATABASE_URL": "postgresql+asyncpg://unused.invalid/tradeos",
            "TRADEOS_TENANT_ID": str(tenant),
            "TRADEOS_DEV_MODE": "true",
            "TRADEOS_CORS_ALLOWED_ORIGINS": '["http://127.0.0.1:4173"]',
            "TRADEOS_API_RETRY_AFTER_SECONDS": "30",
            "TRADEOS_HANDOFF_POLICY": (
                '{"sla_seconds":300,"backlog_threshold":20,'
                '"t1_seconds":120,"t2_seconds":180}'
            ),
            "TRADEOS_SCORING_POLICY": (
                '{"version":"phase1-v1","currency":"USD",'
                '"value_band_boundaries":["1000","5000"],'
                '"bucket_map":{"1":"low","2":"low","3":"mid",'
                '"4":"mid","5":"high","6":"high","7":"high"}}'
            ),
            "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
            "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN",
            "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_FINGERPRINT_KEY",
            "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
            "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
                '{"2026-v1":"UNSUBSCRIBE_HMAC_CURRENT"}'
            ),
            "TRADEOS_TOOL_LEASE_SECONDS": "30",
        }
    )


async def _seed_attempt(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
) -> MessageSendPreflight:
    models = importlib.import_module("domains.outreach.models")
    campaign = CampaignId(new_id("cmp"))
    enrollment = EnrollmentId(new_id("enr"))
    attempt = MessageAttemptId(new_id("mat"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    identity = SendingIdentityId(new_id("sid"))
    employee = EmployeeId(new_id("emp"))
    boundary = models.CampaignBoundary(
        markets=["US"],
        target_entity_types=["importer"],
        allowed_categories=["hardware"],
        sender_identity_ids=[identity],
        steps=[models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0)],
        daily_new_contact_limit=2,
        daily_total_message_limit=3,
        handoff_triggers=[],
        stop_on_reply=True,
    )
    campaign_model = models.Campaign(
        tenant,
        campaign,
        models.CampaignState.DRAFT,
        1,
        employee,
        NOW,
    )
    version = models.CampaignVersion(
        tenant,
        campaign,
        1,
        "Unsubscribe API integration",
        boundary,
        employee,
        NOW,
    )
    enrollment_model = models.Enrollment(
        tenant,
        enrollment,
        campaign,
        1,
        account,
        contact,
        identity,
        models.EnrollmentState.IN_SEQUENCE,
        1,
        NOW,
        NOW,
        None,
        None,
        IdempotencyKey("unsubscribe-enrollment-key"),
    )
    attempt_model = models.MessageAttempt(
        tenant,
        attempt,
        MessageId(new_id("msg")),
        campaign,
        enrollment,
        1,
        1,
        identity,
        IdempotencyKey("unsubscribe-attempt-key"),
        models.MessageAttemptState.RESERVED,
        None,
        None,
        NOW,
        NOW,
    )
    async with SqlAlchemyOutreachUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        await uow.campaigns.add(campaign_model, version)
        await uow.enrollments.insert_if_absent(enrollment_model)
        await uow.attempts.create_if_absent(attempt_model)
    return MessageSendPreflight(
        tenant,
        attempt,
        campaign,
        enrollment,
        account,
        contact,
        identity,
        1,
        1,
        IdempotencyKey("unsubscribe-send-key"),
    )


async def test_twenty_post_requests_consume_once_and_stop_enrollment(
    db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_engine_from(str(db_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    try:
        preflight = await _seed_attempt(factory, tenant)
        dependencies = build_phase1_dependencies(
            _settings(tenant),
            factory,
            now=lambda: NOW,
            secret_resolver=_Secrets(),
        )
        link = await dependencies.unsubscribe_service.issue(tenant, preflight)
        token = link.url.rsplit("/", 1)[1]
        app = create_app(
            settings=ApiSettings(
                tenant_id=str(tenant),
                dev_mode=True,
                retry_after_seconds=30,
            ),
            dependencies=dependencies,
        )
        caplog.clear()
        caplog.set_level(logging.INFO, logger="security.authorization")

        async def consume() -> int:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/unsubscribe/{token}",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    content=b"List-Unsubscribe=One-Click",
                )
            assert response.content == b""
            return response.status_code

        assert Counter(await asyncio.gather(*(consume() for _ in range(20)))) == {
            204: 20
        }

        rows = importlib.import_module("infra.db.tables")
        async with factory() as session:
            suppression_count = await session.scalar(
                select(func.count())
                .select_from(rows.OutreachSuppressionRow)
                .where(rows.OutreachSuppressionRow.tenant_id == str(tenant))
            )
            action_count = await session.scalar(
                select(func.count())
                .select_from(rows.OutreachActionRow)
                .where(rows.OutreachActionRow.tenant_id == str(tenant))
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(rows.OutboxEventRow)
                .where(
                    rows.OutboxEventRow.tenant_id == str(tenant),
                    rows.OutboxEventRow.event_type == "SuppressionAdded",
                )
            )
            stored_enrollment = await session.scalar(
                select(rows.OutreachEnrollmentRow).where(
                    rows.OutreachEnrollmentRow.tenant_id == str(tenant),
                    rows.OutreachEnrollmentRow.enrollment_id
                    == str(preflight.enrollment_id),
                )
            )
            stored_token = await session.scalar(
                select(rows.UnsubscribeTokenRow).where(
                    rows.UnsubscribeTokenRow.tenant_id == str(tenant)
                )
            )
            stored_suppression = await session.scalar(
                select(rows.OutreachSuppressionRow).where(
                    rows.OutreachSuppressionRow.tenant_id == str(tenant)
                )
            )
        assert (suppression_count, action_count, event_count) == (1, 2, 1)
        assert stored_enrollment is not None
        assert (
            stored_enrollment.state,
            stored_enrollment.stop_reason,
            stored_enrollment.next_send_at,
        ) == ("stopped_suppressed", "suppression", None)
        assert stored_token is not None and stored_token.consumed_at == NOW
        assert stored_suppression is not None
        assert (
            stored_suppression.contact_point_id,
            stored_suppression.account_id,
            stored_suppression.reason,
            stored_suppression.source_ref,
            stored_suppression.idempotency_key,
        ) == (
            str(preflight.contact_point_id),
            None,
            "unsubscribe",
            stored_token.nonce_sha256.hex(),
            stored_token.nonce_sha256.hex(),
        )
        allows = [
            record
            for record in caplog.records
            if record.name == "security.authorization"
            and getattr(record, "action", None) == "suppression:add"
            and not str(getattr(record, "rule", "")).startswith("deny:")
        ]
        assert len(allows) == 1
        assert (
            getattr(allows[0], "actor", None),
            getattr(allows[0], "tenant_id", None),
            getattr(allows[0], "scope", None),
        ) == (
            "system:one-click-unsubscribe",
            str(tenant),
            "system",
        )
    finally:
        await engine.dispose()
