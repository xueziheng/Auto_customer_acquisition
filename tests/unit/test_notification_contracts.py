"""通知任务与站内收件箱的公开契约。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from notification_gateway.models import NotificationPriority
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId


def _load(name: str):
    try:
        module = "notification_gateway.inbox" if name in {"NotificationCursor", "InAppNotificationServiceImpl", "InboxActor"} else "notification_gateway.jobs"
        return getattr(importlib.import_module(module), name)
    except (AttributeError, ModuleNotFoundError) as exc:
        pytest.fail(f"RED：缺少通知契约 {name}（{exc}）")


def _context():
    NotificationContext = _load("NotificationContext")
    NotificationKind = _load("NotificationKind")
    return NotificationContext(NotificationKind.HANDOFF_ESCALATION, "han_01", None, None, 1)


def test_context_rejects_free_form_and_secret_markers() -> None:
    """阻止凭证形态或自由映射进入可持久化通知上下文。"""
    NotificationContext = _load("NotificationContext")
    NotificationKind = _load("NotificationKind")
    with pytest.raises(ValidationError, match="通知上下文无效"):
        NotificationContext(NotificationKind.SENDING_IDENTITY_SUSPENDED, "Bearer-private", None, None, None)
    with pytest.raises(ValidationError, match="通知上下文无效"):
        NotificationContext(NotificationKind.HANDOFF_ESCALATION, {"id": "han_01"}, None, None, 1)  # type: ignore[arg-type]


def test_context_and_cursor_do_not_expose_values_in_repr() -> None:
    """防止 ID、分页游标或其它通知上下文被日志默认 repr 泄露。"""
    NotificationCursor = _load("NotificationCursor")
    assert "han_01" not in repr(_context())
    assert "not_01" not in repr(NotificationCursor(datetime(2026, 8, 14, tzinfo=UTC), "not_01"))


def test_notification_rejects_untyped_context_invalid_priority_and_non_utc_time() -> None:
    """公共通知仅接受 typed context、枚举优先级与 UTC 时间。"""
    from notification_gateway.models import Notification

    with pytest.raises(ValidationError):
        Notification(TenantId("tn_01"), EmployeeId("emp_01"), "urgent", "标题", _context(), "Event", "key")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Notification(TenantId("tn_01"), EmployeeId("emp_01"), NotificationPriority.URGENT, "标题", {"kind": "x"}, "Event", "key")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Notification(TenantId("tn_01"), EmployeeId("emp_01"), NotificationPriority.URGENT, "标题", _context(), "Event", "key", due_at=datetime(2026, 8, 14, tzinfo=timezone(timedelta(hours=8))))


@pytest.mark.asyncio
async def test_inbox_service_rejects_foreign_actor_and_bad_limits() -> None:
    """员工不可越租户读收件箱，且分页大小拒绝 bool、零及超限。"""
    InAppNotificationServiceImpl = _load("InAppNotificationServiceImpl")
    InboxActor = _load("InboxActor")

    class Store:
        async def list_for_recipient(self, *args: object, **kwargs: object) -> tuple[object, ...]:
            return ()

        async def mark_read(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("不应读取")

    service = InAppNotificationServiceImpl(Store(), now=lambda: datetime(2026, 8, 14, tzinfo=UTC))
    actor = InboxActor(TenantId("tn_other"), EmployeeId("emp_01"))
    with pytest.raises(TenantIsolationViolation):
        await service.list_notifications(TenantId("tn_01"), actor=actor, limit=50, before=None)
    good_actor = InboxActor(TenantId("tn_01"), EmployeeId("emp_01"))
    for limit in (0, 101, True):
        with pytest.raises(ValidationError):
            await service.list_notifications(TenantId("tn_01"), actor=good_actor, limit=limit, before=None)


@pytest.mark.asyncio
async def test_inbox_service_uses_utc_now_for_mark_read() -> None:
    """已读时间必须由受控 UTC 时钟产生，不接受调用方注入。"""
    InAppNotificationServiceImpl = _load("InAppNotificationServiceImpl")
    InboxActor = _load("InboxActor")
    now = datetime(2026, 8, 14, tzinfo=UTC)
    seen: list[datetime] = []

    class Store:
        async def list_for_recipient(self, *args: object, **kwargs: object) -> tuple[object, ...]:
            return ()

        async def mark_read(self, *args: object, **kwargs: object) -> object:
            seen.append(kwargs["read_at"])  # type: ignore[arg-type]
            return object()

    service = InAppNotificationServiceImpl(Store(), now=lambda: now)
    await service.mark_read(TenantId("tn_01"), "not_01", actor=InboxActor(TenantId("tn_01"), EmployeeId("emp_01")))
    assert seen == [now]
