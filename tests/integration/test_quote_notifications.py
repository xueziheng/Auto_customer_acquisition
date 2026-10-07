"""真实PG job→claim→固定模板→原路由→站内；受控email断言零调用。"""

import importlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.notification_worker.runtime import NotificationRoutingPolicy
from infra.db.repositories.in_app_notifications import PostgresInAppNotificationStore
from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from notification_gateway.channels.in_app import InAppChannel
from notification_gateway.router import NotificationRouter
from notification_gateway.templates import FixedNotificationTemplateRenderer
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 - pytest fixture
)
from tests.unit.test_quote_notifications import NOW


@pytest.mark.parametrize("recipient", ["boss-runtime", "a" * 32, new_id("emp")])
async def test_quote_result_persists_once_with_cross_tenant_isolation_and_zero_email(
    unit_engine, recipient
):
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    tenant, other, quote, run = new_id("tn"), new_id("tn"), new_id("quo"), new_id("run")
    jobs = PostgresNotificationJobStore(factory, now=lambda: NOW)
    assert importlib.util.find_spec("apps.scheduler_worker.quote_notifications"), (
        "缺少持久报价通知适配"
    )
    module = importlib.import_module("apps.scheduler_worker.quote_notifications")
    notifier = module.NotificationJobQuoteApprovalNotifier(
        jobs, tenant_id=tenant, now=lambda: NOW
    )
    kwargs = {
        "run_id": run,
        "recipient_id": recipient,
        "outcome": "approved",
        "idempotency_key": f"quote-approval-notify:{run}:approved",
    }
    await notifier.notify(tenant, quote, **kwargs)
    await notifier.notify(tenant, quote, **kwargs)
    with pytest.raises(TenantIsolationViolation):
        await notifier.notify(other, quote, **kwargs)
    assert await jobs.claim_due(other, limit=10, lease_owner="quote-test") == ()
    claims = await jobs.claim_due(tenant, limit=10, lease_owner="quote-test")
    assert len(claims) == 1
    rendered = FixedNotificationTemplateRenderer().render(claims[0])
    store = PostgresInAppNotificationStore(factory)

    class NoEmail:
        name = "email"
        calls = 0

        async def deliver(self, notification):
            self.calls += 1
            pytest.fail("LOW报价通知不能投邮件")

    email = NoEmail()
    router = NotificationRouter(
        PostgresNotificationDedupStore(factory, now=lambda: NOW),
        NotificationRoutingPolicy(),
    )
    router.register_channel(InAppChannel(store, now=lambda: NOW))
    router.register_channel(email)
    await router.dispatch(rendered)
    await router.dispatch(rendered)
    assert email.calls == 0
    assert await jobs.complete(
        tenant, claims[0].job_id, claim_token=claims[0].claim_token
    )
    assert await jobs.claim_due(tenant, limit=10, lease_owner="quote-test") == ()
    notifications = await store.list_for_recipient(
        tenant, recipient, limit=10, before=None
    )
    assert len(notifications) == 1
    assert notifications[0].relative_link == f"/costing-quotes/quotes/{quote}"
    assert notifications[0].context.reason_code == "approved"
    assert await store.list_for_recipient(other, recipient, limit=10, before=None) == ()
