"""真人绑定、当前权限与原位恢复；技术管理不形成客户需求事实。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from domains.conversations.service import require_inbound_review_access
from domains.sending_identity.service import (
    Actor,
    ScopeLevel,
    SendingIdentityScope,
    SendingIdentityService,
)
from shared.schemas.email_inbound import ArchivedInboundRaw, InboundRoute
from shared.schemas.identifiers import EmployeeId, SendingIdentityId, TenantId
from shared.schemas.quote_facts import QuoteEmployeeFact
from workflows.reply_qualification.inbound_contracts import (
    InboundCursor,
    InboundPageError,
    InboundReviewView,
    InboundStatus,
)


class InboundManagementStore(Protocol):
    async def read_cursor(self) -> InboundCursor | None: ...
    async def bind(
        self,
        route: InboundRoute,
        initial: str,
        started: datetime,
        after_epoch: int,
        confirmed_by: str,
    ) -> InboundCursor: ...
    async def retry(self, expected_version: int) -> InboundCursor: ...
    async def list_reviews(
        self, *, limit: int, after: str | None
    ) -> tuple[InboundReviewView, ...]: ...
    async def review_raw(self, review_id: str) -> ArchivedInboundRaw: ...


class InboundEmployeeReader(Protocol):
    async def read(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> QuoteEmployeeFact: ...


class InboundRawReader(Protocol):
    async def read(
        self, tenant_id: TenantId, employee_id: EmployeeId, review_id: str
    ) -> bytes: ...


class InboundManagement:
    def __init__(
        self,
        tenant_id: TenantId,
        store: InboundManagementStore,
        employees: InboundEmployeeReader,
        sending: SendingIdentityService,
        initial: Callable[[SendingIdentityId, datetime], tuple[InboundRoute, str, int]],
        *,
        now: Callable[[], datetime],
    ):
        (
            self._tenant,
            self._store,
            self._employees,
            self._sending,
            self._initial,
            self._now,
        ) = tenant_id, store, employees, sending, initial, now

    async def _authorize(
        self, tenant: TenantId, employee: EmployeeId, action: Literal["read", "retry"]
    ) -> QuoteEmployeeFact:
        if tenant != self._tenant:
            from shared.errors import PermissionDenied

            raise PermissionDenied("入站待核对权限拒绝")
        actor = await self._employees.read(tenant, employee)
        if actor.employee_id != employee:
            raise InboundPageError("page_integrity")
        require_inbound_review_access(tenant, actor, action=action)
        return actor

    def status_of(self, cursor: InboundCursor | None) -> InboundStatus:
        if cursor is None:
            return InboundStatus(state="disabled")
        state = (
            "waiting"
            if cursor.next_retry_at is not None and self._now() < cursor.next_retry_at
            else "blocked"
            if cursor.blocked_reason is not None and cursor.next_retry_at is None
            else "active"
        )
        return InboundStatus.model_validate(
            {
                "state": state,
                "version": cursor.version,
                "identity_id": cursor.route.configured_identity_id,
                "reason": cursor.blocked_reason,
                "last_succeeded_at": cursor.last_succeeded_at,
                "next_retry_at": cursor.next_retry_at,
            }
        )

    async def status(self, tenant: TenantId, employee: EmployeeId) -> InboundStatus:
        await self._authorize(tenant, employee, "read")
        return self.status_of(await self._store.read_cursor())

    async def bind(
        self, tenant: TenantId, employee: EmployeeId, identity_id: SendingIdentityId
    ) -> InboundStatus:
        actor = await self._authorize(tenant, employee, "read")
        await self._sending.authorize_inbound_binding(
            tenant,
            identity_id,
            actor=Actor(
                str(actor.employee_id),
                SendingIdentityScope(level=ScopeLevel.TENANT),
                actor.role,
            ),
        )
        now = self._now()
        route, initial, after = self._initial(identity_id, now)
        if route.tenant_id != tenant:
            raise InboundPageError("binding_conflict")
        return self.status_of(
            await self._store.bind(route, initial, now, after, str(actor.employee_id))
        )

    async def retry(
        self, tenant: TenantId, employee: EmployeeId, expected_version: int
    ) -> InboundStatus:
        await self._authorize(tenant, employee, "retry")
        return self.status_of(await self._store.retry(expected_version))

    async def reviews(
        self, tenant: TenantId, employee: EmployeeId, *, limit: int, after: str | None
    ) -> tuple[InboundReviewView, ...]:
        await self._authorize(tenant, employee, "read")
        return await self._store.list_reviews(limit=limit, after=after)

    async def authorize_raw(
        self, tenant: TenantId, employee: EmployeeId, review_id: str
    ) -> ArchivedInboundRaw:
        await self._authorize(tenant, employee, "read")
        return await self._store.review_raw(review_id)
