"""报价结果仅固定LOW元数据；旧kind员工编号门保持不变。"""

import importlib
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from notification_gateway.jobs import (
    NotificationContext,
    NotificationJobClaim,
    NotificationKind,
)
from notification_gateway.models import NotificationPriority
from notification_gateway.templates import FixedNotificationTemplateRenderer
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import new_id

TENANT, QUOTE, RUN = new_id("tn"), new_id("quo"), new_id("run")
NOW = datetime(2026, 8, 29, tzinfo=UTC)


def notifier(channel, port):
    name = f"apps.{'api.composition' if channel == 'api' else 'scheduler_worker'}.quote_notifications"
    assert importlib.util.find_spec(name), "缺少报价结果真实通知适配"
    module = importlib.import_module(name)
    return (
        module.RuntimeQuoteApprovalNotifier(port, tenant_id=TENANT)
        if channel == "api"
        else module.NotificationJobQuoteApprovalNotifier(
            port, tenant_id=TENANT, now=lambda: NOW
        )
    )


@pytest.mark.parametrize("channel", ["api", "worker"])
@pytest.mark.parametrize("outcome", ["approved", "rejected", "expired", "obsolete"])
async def test_notifier_uses_only_fixed_metadata_and_original_key(channel, outcome):
    port = AsyncMock()
    service = notifier(channel, port)
    key = f"quote-approval-notify:{RUN}:{outcome}"
    await service.notify(
        TENANT,
        QUOTE,
        run_id=RUN,
        recipient_id="boss-runtime",
        outcome=outcome,
        idempotency_key=key,
    )
    value = (port.dispatch if channel == "api" else port.enqueue).await_args.args[0]
    assert value.tenant_id == TENANT and value.recipient == "boss-runtime"
    assert value.priority is NotificationPriority.LOW and value.dedup_key == key
    assert value.source_event == "QuoteApprovalResult"
    assert value.context == NotificationContext(
        NotificationKind.QUOTE_APPROVAL_RESULT, QUOTE, RUN, outcome, None
    )
    if channel == "worker":
        await service.notify(
            TENANT,
            QUOTE,
            run_id=RUN,
            recipient_id="boss-runtime",
            outcome=outcome,
            idempotency_key=key,
        )
        assert (
            port.enqueue.await_args.args[0].source_event_fingerprint
            == value.source_event_fingerprint
        )
    else:
        assert value.link == f"/costing-quotes/quotes/{QUOTE}"


@pytest.mark.parametrize("channel", ["api", "worker"])
@pytest.mark.parametrize(
    "recipient",
    [
        "员工",
        "employee space",
        " bearer",
        "token-user",
        "sk-test",
        "a" * 33,
        "a" * 40,
        "emp_x\n",
    ],
)
async def test_invalid_recipient_is_rejected_before_notification_io(channel, recipient):
    port = AsyncMock()
    service = notifier(channel, port)
    with pytest.raises(ValidationError):
        await service.notify(
            TENANT,
            QUOTE,
            run_id=RUN,
            recipient_id=recipient,
            outcome="approved",
            idempotency_key=f"quote-approval-notify:{RUN}:approved",
        )
    assert port.mock_calls == []


@pytest.mark.parametrize("channel", ["api", "worker"])
async def test_notifier_is_bound_to_runtime_tenant(channel):
    port = AsyncMock()
    service = notifier(channel, port)
    with pytest.raises(TenantIsolationViolation):
        await service.notify(
            new_id("tn"),
            QUOTE,
            run_id=RUN,
            recipient_id="boss-runtime",
            outcome="approved",
            idempotency_key=f"quote-approval-notify:{RUN}:approved",
        )
    assert port.mock_calls == []


def claim():
    assert hasattr(NotificationKind, "QUOTE_APPROVAL_RESULT"), "缺少报价结果通知kind"
    return NotificationJobClaim(
        new_id("njb"),
        TENANT,
        "boss-runtime",
        NotificationPriority.LOW,
        NotificationContext(
            NotificationKind.QUOTE_APPROVAL_RESULT, QUOTE, RUN, "approved", None
        ),
        "QuoteApprovalResult",
        f"quote-approval-notify:{RUN}:approved",
        new_id("njc"),
        1,
    )


@pytest.mark.parametrize("recipient", ["boss-runtime", "a" * 32, new_id("emp")])
def test_new_fixed_template_accepts_only_approved_short_identity_exception(recipient):
    value = FixedNotificationTemplateRenderer().render(
        replace(claim(), recipient=recipient)
    )
    assert value.recipient == recipient and value.priority is NotificationPriority.LOW
    assert value.link == f"/costing-quotes/quotes/{QUOTE}"


@pytest.mark.parametrize(
    "changes",
    [
        {"source_event": "ApprovalDecided"},
        {"priority": NotificationPriority.NORMAL},
        {"recipient": "a" * 33},
        {"recipient": "secret-user"},
        {"recipient": "员工"},
    ],
)
def test_quote_template_rejects_changed_claim_metadata(changes):
    with pytest.raises(ValidationError):
        FixedNotificationTemplateRenderer().render(replace(claim(), **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": NotificationKind.APPROVAL_DECIDED},
        {"reason_code": "ready"},
        {"primary_id": new_id("apr")},
        {"secondary_id": None},
        {"secondary_id": new_id("emp")},
        {"level": 1},
    ],
)
def test_quote_template_rejects_forged_kind_outcome_and_binding(changes):
    value = claim()
    with pytest.raises(ValidationError):
        FixedNotificationTemplateRenderer().render(
            replace(value, context=replace(value.context, **changes))
        )
