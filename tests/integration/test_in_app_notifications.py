"""站内通知 PostgreSQL 收件箱隔离与只读内容契约。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from notification_gateway.models import NotificationPriority
from shared.schemas.identifiers import EmployeeId, TenantId


def _load(name: str):
    try:
        return getattr(importlib.import_module("notification_gateway.inbox"), name)
    except (AttributeError, ModuleNotFoundError) as exc:
        pytest.fail(f"RED：缺少站内通知契约（{exc}）")


def _context():
    from notification_gateway.jobs import NotificationContext, NotificationKind
    return NotificationContext(NotificationKind.HANDOFF_ESCALATION, "han_01", None, None, 1)


@pytest.mark.asyncio
async def test_inbox_append_is_idempotent_recipient_scoped_and_read_once(db_url: str) -> None:
    """source job 唯一，跨租户不可读，内容不可改且 read_at 只可单调设置一次。"""
    from infra.db.session import create_engine_from
    try:
        from infra.db.repositories.in_app_notifications import (
            PostgresInAppNotificationStore,
        )
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：站内通知仓储未创建（{exc}）")
    InAppNotification = _load("InAppNotification")
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    store = PostgresInAppNotificationStore(factory)
    ids = importlib.import_module("shared.schemas.identifiers")
    NotificationId = ids.NotificationId
    NotificationJobId = ids.NotificationJobId
    item = InAppNotification(NotificationId("not_01"), TenantId("tn_inbox"), EmployeeId("emp_inbox"), NotificationPriority.URGENT, "接管提醒", _context(), "/crm/handoffs/han_01", NotificationJobId("njb_01"), datetime(2026, 8, 14, tzinfo=UTC))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO notification_jobs (tenant_id, notification_job_id, source_event_fingerprint, source_event, recipient_employee_id, priority, context_kind, primary_id, dedup_key, available_at, attempt_count, created_at) VALUES ('tn_inbox','njb_01', :fingerprint,'Event','emp_inbox','urgent','handoff_escalation','han_01','key',now(),0,now())"), {"fingerprint": "b" * 64})
        assert await store.append(item) is True
        assert await store.append(item) is False
        assert await store.list_for_recipient(TenantId("tn_other"), EmployeeId("emp_inbox"), limit=50, before=None) == ()
        one = (await store.list_for_recipient(TenantId("tn_inbox"), EmployeeId("emp_inbox"), limit=50, before=None))[0]
        assert one.title == "接管提醒" and one.read_at is None
        read = await store.mark_read(TenantId("tn_inbox"), EmployeeId("emp_inbox"), NotificationId("not_01"), read_at=datetime(2026, 8, 14, 1, tzinfo=UTC))
        assert read.read_at == datetime(2026, 8, 14, 1, tzinfo=UTC)
        again = await store.mark_read(TenantId("tn_inbox"), EmployeeId("emp_inbox"), NotificationId("not_01"), read_at=datetime(2026, 8, 14, 2, tzinfo=UTC))
        assert again.read_at == datetime(2026, 8, 14, 1, tzinfo=UTC)
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError):
                await conn.execute(text("UPDATE in_app_notifications SET title='mutated' WHERE tenant_id='tn_inbox' AND notification_id='not_01'"))
    finally:
        await engine.dispose()
