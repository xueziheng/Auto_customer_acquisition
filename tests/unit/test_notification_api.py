"""Internal notification inbox API：仅本人收件箱，跨收件人拒绝且零写入。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import ConfiguredApiDependencies
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from notification_gateway.inbox import InAppNotificationView, InboxActor
from notification_gateway.jobs import NotificationContext, NotificationKind
from notification_gateway.models import NotificationPriority
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    NotificationId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
BOSS = EmployeeId(new_id("emp"))
SALES = EmployeeId(new_id("emp"))
NOTIFICATION = NotificationId(new_id("ntf"))
OTHER_NOTIFICATION = NotificationId(new_id("ntf"))


def _employee(employee_id: EmployeeId, role: str) -> EmployeeView:
    return EmployeeView(
        employee_id=employee_id,
        tenant_id=TENANT,
        name="测试员工",
        role=role,
        manager_id=None,
        is_active=True,
    )


class _EmployeeService:
    def __init__(self, employee: EmployeeView) -> None:
        self.employee = employee

    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: EmployeeActor
    ) -> EmployeeView:
        del tenant_id, actor
        assert employee_id == self.employee.employee_id
        return self.employee

    async def list_active(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[EmployeeView]:
        del tenant_id, actor
        return [self.employee]


class _EmployeeScope:
    def __init__(self, service: _EmployeeService) -> None:
        self.service = service

    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId) -> AsyncIterator[_EmployeeService]:
        assert tenant_id == TENANT
        yield self.service


def _view(notification_id: NotificationId) -> InAppNotificationView:
    return InAppNotificationView(
        notification_id=notification_id,
        tenant_id=TENANT,
        priority=NotificationPriority.NORMAL,
        title="测试通知",
        context=NotificationContext(
            NotificationKind.HANDOFF_ESCALATION,
            str(new_id("hnd")),
            None,
            reason_code="t1",
            level=None,
        ),
        relative_link="/crm/handoffs/handoff-1",
        created_at=NOW,
        read_at=None,
    )


class _Inbox:
    def __init__(self) -> None:
        self.list_calls: list[tuple[TenantId, InboxActor, int, object]] = []
        self.read_calls: list[tuple[TenantId, InboxActor, NotificationId]] = []

    async def list_notifications(
        self,
        tenant_id: TenantId,
        *,
        actor: InboxActor,
        limit: int,
        before: object | None,
    ) -> tuple[InAppNotificationView, ...]:
        self.list_calls.append((tenant_id, actor, limit, before))
        if actor.employee_id != BOSS:
            return ()
        return (_view(NOTIFICATION),)

    async def mark_read(
        self,
        tenant_id: TenantId,
        notification_id: NotificationId,
        *,
        actor: InboxActor,
    ) -> InAppNotificationView:
        self.read_calls.append((tenant_id, actor, notification_id))
        if actor.employee_id != BOSS or notification_id != NOTIFICATION:
            raise ValidationError("通知不存在")
        return _view(NOTIFICATION)


def _app(employee: EmployeeView):
    from apps.api.main import ApiSettings, create_app

    inbox = _Inbox()
    dependencies = ConfiguredApiDependencies(
        opportunities=object(),
        outreach=object(),
        sending_identities=object(),
        tool_gateway=_ToolGateway(),
        delivery_materials=_DeliveryMaterials(),
        unsubscribe_links=_UnsubscribeLinks(),
        unsubscribe_service=_UnsubscribeService(),
        employees=_EmployeeScope(_EmployeeService(employee)),
        opportunity_authorizer=object(),
        employee_authorizer=object(),
        workflow_engine=object(),
        outbox_deliverer=object(),
        notification_router=object(),
        notification_dedup_store=object(),
        employee_lookup_actor=EmployeeActor(
            actor_id="system:api-identity", scope=EmployeeScope.SYSTEM, role="system"
        ),
        outreach_authorizer=object(),
        sending_identity_authorizer=object(),
        campaign_scope_resolver=object(),
        in_app_notifications=inbox,
    )
    app = create_app(
        settings=ApiSettings(tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17),
        dependencies=dependencies,
    )
    return app, inbox


class _ToolGateway:
    async def invoke(self, ctx: object) -> object:
        del ctx
        raise AssertionError("本测试不应调用手工发送 Gateway")


class _DeliveryMaterials:
    async def resolve(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不应解析发送材料")


class _UnsubscribeLinks:
    async def build(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("本测试不应生成退订链接")


class _UnsubscribeService:
    async def issue(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不应签发退订 token")

    async def consume(self, opaque_token: str) -> bool:
        del opaque_token
        return False



class _Client:
    def __init__(self, app: Any) -> None:
        self._app = app

    def get(self, path: str, *, headers: dict[str, str]) -> Response:
        return self._request("GET", path, headers=headers)

    def post(self, path: str, *, headers: dict[str, str]) -> Response:
        return self._request("POST", path, headers=headers)

    def _request(self, method: str, path: str, *, headers: dict[str, str]) -> Response:
        async def run() -> Response:
            transport = ASGITransport(app=self._app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, headers=headers)

        return asyncio.run(run())


def _headers(employee_id: EmployeeId) -> dict[str, str]:
    return {"X-Tenant-Id": str(TENANT), "X-Employee-Id": str(employee_id)}


def test_inbox_list_returns_only_own_notifications() -> None:
    app, inbox = _app(_employee(BOSS, "boss"))
    response = _Client(app).get("/notifications?limit=10", headers=_headers(BOSS))
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["notification_id"] == str(NOTIFICATION)
    assert set(body[0]) == {
        "notification_id", "tenant_id", "priority", "title", "context",
        "relative_link", "created_at", "read_at",
    }
    tenant, actor, limit, before = inbox.list_calls[0]
    assert (tenant, actor.tenant_id, actor.employee_id, limit) == (
        TENANT, TENANT, BOSS, 10,
    )
    assert before is None


def test_inbox_ignores_recipient_query_input() -> None:
    app, inbox = _app(_employee(BOSS, "boss"))
    other = EmployeeId(new_id("emp"))
    response = _Client(app).get(
        f"/notifications?limit=10&recipient={other}", headers=_headers(BOSS)
    )
    assert response.status_code == 200
    _, actor, _, _ = inbox.list_calls[0]
    assert actor.employee_id == BOSS


def test_other_employee_inbox_is_empty_and_cross_recipient_read_denied_without_write() -> None:
    app, inbox = _app(_employee(SALES, "sales"))
    client = _Client(app)
    listed = client.get("/notifications?limit=10", headers=_headers(SALES))
    assert listed.status_code == 200
    assert listed.json() == []
    read = client.post(f"/notifications/{NOTIFICATION}/read", headers=_headers(SALES))
    assert read.status_code == 400
    assert read.json() == {"code": "validation_error", "message": "请求参数无效"}
    # 服务以本人 actor 被调用并在写入前拒绝：收件人恒为请求身份本人。
    _, actor, notification_id = inbox.read_calls[0]
    assert actor.employee_id == SALES
    assert notification_id == NOTIFICATION


def test_mark_read_uses_own_recipient_and_returns_view() -> None:
    app, inbox = _app(_employee(BOSS, "boss"))
    response = _Client(app).post(
        f"/notifications/{NOTIFICATION}/read", headers=_headers(BOSS)
    )
    assert response.status_code == 200
    assert response.json()["notification_id"] == str(NOTIFICATION)
    tenant, actor, notification_id = inbox.read_calls[0]
    assert (tenant, actor.employee_id, notification_id) == (
        TENANT, BOSS, NOTIFICATION,
    )


def test_mark_read_rejects_invalid_notification_id() -> None:
    app, inbox = _app(_employee(BOSS, "boss"))
    response = _Client(app).post(
        "/notifications/not-an-id/read", headers=_headers(BOSS)
    )
    assert response.status_code == 400
    assert inbox.read_calls == []
