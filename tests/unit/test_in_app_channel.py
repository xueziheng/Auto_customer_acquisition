"""站内渠道是持久收件箱前最后一道安全边界。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from notification_gateway.jobs import NotificationContext, NotificationKind
from notification_gateway.models import Notification, NotificationPriority
from shared.errors import PolicyViolation
from shared.schemas.identifiers import (
    EmployeeId,
    NotificationJobId,
    TenantId,
    new_id,
)

_NOW = datetime(2026, 8, 14, tzinfo=UTC)
_TENANT = TenantId(new_id("tn"))
_EMPLOYEE = EmployeeId(new_id("emp"))


def _channel(store: object):
    try:
        Channel = importlib.import_module(
            "notification_gateway.channels.in_app"
        ).InAppChannel
    except (AttributeError, ModuleNotFoundError) as exc:
        pytest.fail(f"RED：站内通知渠道尚未实现（{exc}）")
    return Channel(store, now=lambda: _NOW, id_factory=lambda prefix: f"{prefix}_fixed")


class _Store:
    def __init__(self) -> None:
        self.items: list[object] = []

    async def append(self, notification: object) -> bool:
        self.items.append(notification)
        return True


def _notification(**changes: object) -> Notification:
    values = {
        "tenant_id": _TENANT,
        "recipient": _EMPLOYEE,
        "priority": NotificationPriority.URGENT,
        "title": "人工接管提醒",
        "context": NotificationContext(
            NotificationKind.HANDOFF_ESCALATION,
            "han_01",
            "opp_01",
            "owner",
            None,
        ),
        "source_event": "HandoffEscalationNotice",
        "dedup_key": "handoff:01:owner",
        "next_step": "处理人工接管任务",
        "link": "/crm/handoffs/han_01",
        "source_job_id": NotificationJobId(new_id("njb")),
    }
    values.update(changes)
    return Notification(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_in_app_channel_appends_typed_job_backed_notification() -> None:
    """站内行必须保留任务来源和 typed context。"""
    store = _Store()
    notification = _notification()
    await _channel(store).deliver(notification)
    item = store.items[0]
    assert (
        item.tenant_id,
        item.recipient,
        item.priority,
        item.title,
        item.context,
        item.relative_link,
        item.source_job_id,
        item.created_at,
    ) == (
        notification.tenant_id,
        notification.recipient,
        notification.priority,
        notification.title,
        notification.context,
        notification.link,
        notification.source_job_id,
        _NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"source_job_id": None},
        {"link": "https://evil.test/path"},
        {"link": "//evil.test/path"},
        {"link": "\\\\evil.test\\path"},
        {"title": "pass" + "word='super-secret'"},
        {"tenant_id": TenantId("invalid-tenant")},
        {"recipient": EmployeeId("invalid-employee")},
    ],
)
async def test_in_app_channel_rejects_unsafe_or_unbound_notifications(
    changes: dict[str, object],
) -> None:
    """外链、凭证、非法身份或无 job 通知均不得持久化。"""
    store = _Store()
    with pytest.raises(PolicyViolation, match="站内通知"):
        await _channel(store).deliver(_notification(**changes))
    assert store.items == []


@pytest.mark.asyncio
async def test_in_app_channel_rejects_raw_context_even_if_model_is_forged() -> None:
    """不能靠 Notification 构造器曾经校验过来代替最终渠道校验。"""
    forged = object.__new__(Notification)
    for key, value in _notification().__dict__.items():
        object.__setattr__(forged, key, value)
    object.__setattr__(forged, "context", {"kind": "handoff_escalation"})
    store = _Store()
    with pytest.raises(PolicyViolation, match="站内通知"):
        await _channel(store).deliver(forged)
    assert store.items == []


@pytest.mark.asyncio
async def test_in_app_channel_revalidates_forged_typed_context() -> None:
    """最终渠道不能只靠 isinstance 接受绕过构造校验的 typed 对象。"""
    unsafe_context = object.__new__(NotificationContext)
    object.__setattr__(unsafe_context, "kind", NotificationKind.HANDOFF_QUEUE_BACKLOGGED)
    object.__setattr__(unsafe_context, "primary_id", "handoff_queue")
    object.__setattr__(unsafe_context, "secondary_id", None)
    object.__setattr__(unsafe_context, "reason_code", None)
    object.__setattr__(unsafe_context, "level", 100)
    forged = object.__new__(Notification)
    for key, value in _notification().__dict__.items():
        object.__setattr__(forged, key, value)
    object.__setattr__(forged, "context", unsafe_context)
    store = _Store()
    with pytest.raises(PolicyViolation, match="站内通知"):
        await _channel(store).deliver(forged)
    assert store.items == []
