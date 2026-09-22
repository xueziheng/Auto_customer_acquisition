"""本人邮箱的公开契约与授权规则；不与业务会话、客户或商机混写。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId
from shared.schemas.mailbox import MailAttachment, MailboxPage


class MailboxActor(BaseModel):
    model_config = ConfigDict(frozen=True)
    tenant_id: TenantId
    employee_id: EmployeeId


def require_mailbox_owner(
    actor: MailboxActor, tenant_id: str, owner_id: str, active: bool
) -> None:
    """老板和经理也不能越权读他人的全邮箱。"""
    if not active or actor.tenant_id != tenant_id or actor.employee_id != owner_id:
        raise PermissionDenied("无权访问此邮箱")


class MailboxView(BaseModel):
    mailbox_id: str
    email: str
    phase: Literal["backfill", "catch_up", "synced"]
    message_count: int
    last_synced_at: datetime | None
    last_attempt_at: datetime | None
    failure_code: str | None
    sync_requested: bool


class MailThreadView(BaseModel):
    thread_id: str
    subject: str
    sender: str
    snippet: str
    latest_at: datetime
    message_count: int
    unread: bool


class MailThreadPage(BaseModel):
    items: list[MailThreadView]
    next_offset: int | None


class MailMessageView(BaseModel):
    message_id: str
    thread_id: str
    occurred_at: datetime
    labels: list[str]
    subject: str
    sender: str
    recipients: str
    snippet: str
    body_text: str
    attachments: list[MailAttachment]
    source_sha256: str
    source_url: str


class MailMessagePage(BaseModel):
    items: list[MailMessageView]
    next_offset: int | None


class MailboxCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    mailbox_id: str
    email: str
    cursor: str | None = Field(repr=False)
    revision: int
    phase: Literal["backfill", "catch_up", "synced"]
    sync_requested: bool


class MailboxRepository(Protocol):
    async def register(self, actor: MailboxActor, email: str) -> str: ...
    async def mailboxes(self, actor: MailboxActor) -> list[MailboxView]: ...
    async def threads(
        self,
        actor: MailboxActor,
        mailbox_id: str,
        *,
        search: str,
        label: str | None,
        offset: int,
        limit: int,
    ) -> MailThreadPage: ...
    async def messages(
        self,
        actor: MailboxActor,
        mailbox_id: str,
        thread_id: str,
        *,
        offset: int,
        limit: int,
    ) -> MailMessagePage: ...
    async def checkpoint(
        self, actor: MailboxActor, mailbox_id: str
    ) -> MailboxCheckpoint: ...
    async def apply_page(
        self, actor: MailboxActor, expected: MailboxCheckpoint, page: MailboxPage
    ) -> None: ...
    async def failed(self, actor: MailboxActor, mailbox_id: str, code: str) -> None: ...
    async def request_sync(self, actor: MailboxActor, mailbox_id: str) -> None: ...


class MailboxService:
    """公开用途只允许本人镜像；每个仓储事务再次应用当前员工/owner 规则。"""

    def __init__(self, repository: MailboxRepository):
        self.repository = repository

    async def mailboxes(self, actor: MailboxActor) -> list[MailboxView]:
        return await self.repository.mailboxes(actor)

    async def threads(
        self,
        actor: MailboxActor,
        mailbox_id: str,
        *,
        search: str = "",
        label: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> MailThreadPage:
        self._paging(offset, limit)
        if len(search) > 200 or label not in {
            None,
            "INBOX",
            "SENT",
            "DRAFT",
            "SPAM",
            "TRASH",
            "UNREAD",
            "ARCHIVED",
        }:
            raise ValidationError("邮箱筛选无效")
        return await self.repository.threads(
            actor,
            mailbox_id,
            search=search.strip(),
            label=label,
            offset=offset,
            limit=limit,
        )

    async def messages(
        self,
        actor: MailboxActor,
        mailbox_id: str,
        thread_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> MailMessagePage:
        self._paging(offset, limit)
        return await self.repository.messages(
            actor, mailbox_id, thread_id, offset=offset, limit=limit
        )

    async def request_sync(self, actor: MailboxActor, mailbox_id: str) -> None:
        await self.repository.request_sync(actor, mailbox_id)

    @staticmethod
    def _paging(offset: int, limit: int) -> None:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValidationError("邮箱分页无效")
