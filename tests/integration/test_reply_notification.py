"""真实 PG 回复分类事件到老板站内通知；邮箱为禁止调用的 fixture。"""
from datetime import UTC, datetime
from functools import partial

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.composition_support.employee_readers import employee_service_scope
from apps.notification_worker.runtime import NotificationRoutingPolicy
from apps.scheduler_worker.bootstrap import CurrentNotificationAudience
from apps.scheduler_worker.notification_projection import NotificationProjectionHandler
from domains.conversations.schemas import ReplyCategory
from domains.employees.permissions import Phase1EmployeeAuthorizer, StandardAuditLogger
from infra.db.repositories.in_app_notifications import PostgresInAppNotificationStore
from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.session import create_engine_from
from infra.db.tables import EmployeeRow
from notification_gateway.channels.in_app import InAppChannel
from notification_gateway.router import NotificationRouter
from notification_gateway.templates import FixedNotificationTemplateRenderer
from shared.events.catalog import ReplyReceived
from shared.schemas.identifiers import (
    EmployeeId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)
from tests.integration.test_conversation_reply_work_actions import _service

NOW = datetime(2026, 10, 15, tzinfo=UTC)

@pytest.mark.parametrize("category,corrected", [("clear_interest", False), ("willing_to_continue", False), ("rejection", False), ("needs_review", False), ("clear_interest", True)])
@pytest.mark.parametrize("revoked", [False, True])
async def test_reply_event_current_boss_only_dedup_and_real_in_app(db_url, category, revoked, corrected):
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    boss, manager, sales = [EmployeeId(new_id("emp")) for _ in range(3)]
    try:
        async with factory.begin() as session:
            for employee, role in [(boss, "boss"), (manager, "manager"), (sales, "sales")]:
                session.add(EmployeeRow(employee_id=employee, tenant_id=tenant, name="fixture",
                                        role=role, is_active=True))
        scope = partial(employee_service_scope, factory, now=lambda: NOW,
            authorizer=Phase1EmployeeAuthorizer(tenant), audit=StandardAuditLogger())
        audience = CurrentNotificationAudience(tenant, scope, None)
        conversations = _service(factory, tenant)
        outbound = OutboundMessageId("<qq-test." + "a" * 64 + "@messages.tradeos.invalid>")
        account = ProspectAccountId(new_id("acc"))
        message = await conversations.ingest_inbound(
            tenant, None, account, new_id("art"), f"<{new_id('ext')}@qq.com>",
            NOW, outbound_message_id=outbound,
        )
        await conversations.record_classification(
            tenant, message, ReplyCategory(category), "fixture:v1", outbound_message_id=outbound,
        )
        if corrected:
            from domains.conversations.service import InboxActor, InboxScope
            await conversations.correct_classification(tenant, message, ReplyCategory.REJECTION,
                boss, actor=InboxActor(tenant, boss, "boss", InboxScope.TENANT))
        # 从持久消息取得真实会话，事件不能自行拼接其他客户。
        from sqlalchemy import select

        from infra.db.tables import MessageRow
        async with factory() as session:
            conversation = await session.scalar(select(MessageRow.conversation_id).where(
                MessageRow.tenant_id == tenant, MessageRow.message_id == message,
            ))
        event = ReplyReceived(tenant_id=tenant, occurred_at=NOW, message_id=message,
            conversation_id=conversation, reply_category=category, outbound_message_id=outbound)
        jobs = PostgresNotificationJobStore(factory, now=lambda: NOW)
        handler = NotificationProjectionHandler(tenant_id=tenant, audience=audience,
            jobs=jobs, conversations=conversations, now=lambda: NOW)
        await handler.handle(event)
        await handler.handle(event)
        claims = await jobs.claim_due(tenant, limit=10, lease_owner="fixture-reply")
        assert len(claims) == (0 if category in {"rejection", "needs_review"} or corrected else 1)
        if not claims:
            return
        assert claims[0].recipient == boss
        assert claims[0].context.reason_code == category
        store = PostgresInAppNotificationStore(factory)
        router = NotificationRouter(PostgresNotificationDedupStore(factory, now=lambda: NOW),
                                    NotificationRoutingPolicy())
        router.register_channel(InAppChannel(store, now=lambda: NOW))
        class ForbiddenEmail:
            name = "email"
            async def deliver(self, notification):
                pytest.fail("客户回复通知禁止发送邮箱")
        router.register_channel(ForbiddenEmail())
        if revoked:
            from sqlalchemy import update
            async with factory.begin() as db:
                await db.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == tenant,
                    EmployeeRow.employee_id == boss).values(role="sales"))
        notification = FixedNotificationTemplateRenderer().render(claims[0])
        await router.dispatch(notification)
        await router.dispatch(notification)
        assert await jobs.complete(tenant, claims[0].job_id, claim_token=claims[0].claim_token)
        assert not await jobs.claim_due(tenant, limit=10, lease_owner="after-restart")
        actual = await store.list_for_recipient(tenant, boss, limit=10, before=None)
        assert len(actual) == (0 if revoked else 1)
        if actual:
            assert actual[0].relative_link == f"/inbox/conversations/{conversation}"
        for employee in (manager, sales):
            assert not await store.list_for_recipient(tenant, employee, limit=10, before=None)
    finally:
        await engine.dispose()
