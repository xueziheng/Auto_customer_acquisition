"""账号镜像持久仓储：本人/租户双过滤，消息与检查点整页原子提交。"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.mailbox import (
    MailboxActor,
    MailboxCheckpoint,
    MailboxView,
    MailMessagePage,
    MailMessageView,
    MailThreadPage,
    MailThreadView,
    require_mailbox_owner,
)
from infra.db.base import TenantScopedRepository
from infra.db.mailbox_tables import MailboxAccountRow as Account
from infra.db.mailbox_tables import MailboxMessageRow as Message
from infra.db.tables import EmployeeRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import new_id
from shared.schemas.mailbox import MailboxFailure, MailboxPage


class SqlMailboxRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    async def _active(self, db: AsyncSession, actor: MailboxActor) -> EmployeeRow:
        employee = (
            await db.scalars(
                TenantScopedRepository(actor.tenant_id)
                .scoped_query(EmployeeRow)
                .where(EmployeeRow.employee_id == actor.employee_id)
                .with_for_update(read=True)
            )
        ).one_or_none()
        require_mailbox_owner(
            actor,
            actor.tenant_id,
            actor.employee_id,
            employee is not None and employee.is_active and bool(employee.user_id),
        )

        if employee is None:
            raise PermissionDenied("员工身份不可用")
        return employee

    async def _account(
        self,
        db: AsyncSession,
        actor: MailboxActor,
        mailbox_id: str,
        *,
        lock: bool = False,
    ) -> Account:
        employee = await self._active(db, actor)
        query = (
            TenantScopedRepository(actor.tenant_id)
            .scoped_query(Account)
            .where(
                Account.mailbox_id == mailbox_id,
                Account.employee_id == actor.employee_id,
                Account.user_id == employee.user_id,
            )
        )
        row = (
            await db.scalars(query.with_for_update() if lock else query)
        ).one_or_none()
        if row is None:
            raise PermissionDenied("无权访问此邮箱")
        require_mailbox_owner(
            actor, row.tenant_id, row.employee_id, row.user_id == employee.user_id
        )
        return row

    @staticmethod
    def _checkpoint(row: Account) -> MailboxCheckpoint:
        return MailboxCheckpoint(
            mailbox_id=row.mailbox_id,
            email=row.email,
            cursor=row.cursor,
            revision=row.revision,
            phase=row.phase,
            sync_requested=row.sync_requested,
        )

    async def register(self, actor: MailboxActor, email: str) -> str:
        email = email.strip().casefold()
        if (
            not 3 <= len(email) <= 320
            or email.count("@") != 1
            or any(c.isspace() for c in email)
        ):
            raise ValidationError("邮箱地址无效")
        async with self.sessions() as db, db.begin():
            employee = await self._active(db, actor)
            await db.execute(
                insert(Account)
                .values(
                    tenant_id=actor.tenant_id,
                    employee_id=actor.employee_id,
                    user_id=employee.user_id,
                    mailbox_id=new_id("mbx"),
                    email=email,
                    phase="backfill",
                    revision=0,
                    generation=0,
                    sync_requested=True,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "email"])
            )
            row = (
                await db.scalars(
                    TenantScopedRepository(actor.tenant_id)
                    .scoped_query(Account)
                    .where(Account.email == email)
                )
            ).one()
            require_mailbox_owner(
                actor, row.tenant_id, row.employee_id, row.user_id == employee.user_id
            )
            return row.mailbox_id

    async def mailboxes(self, actor: MailboxActor) -> list[MailboxView]:
        async with self.sessions() as db, db.begin():
            employee = await self._active(db, actor)
            rows = (
                await db.scalars(
                    TenantScopedRepository(actor.tenant_id)
                    .scoped_query(Account)
                    .where(
                        Account.employee_id == actor.employee_id,
                        Account.user_id == employee.user_id,
                    )
                    .order_by(Account.email)
                )
            ).all()
            result = []
            for row in rows:
                count = await db.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.tenant_id == actor.tenant_id,
                        Message.mailbox_id == row.mailbox_id,
                    )
                )
                result.append(
                    MailboxView(
                        mailbox_id=row.mailbox_id,
                        email=row.email,
                        phase=row.phase,
                        message_count=count or 0,
                        last_synced_at=row.last_synced_at,
                        last_attempt_at=row.last_attempt_at,
                        failure_code=row.failure_code,
                        sync_requested=row.sync_requested,
                    )
                )
            return result

    async def checkpoint(
        self, actor: MailboxActor, mailbox_id: str
    ) -> MailboxCheckpoint:
        async with self.sessions() as db, db.begin():
            return self._checkpoint(await self._account(db, actor, mailbox_id))

    async def apply_page(
        self, actor: MailboxActor, expected: MailboxCheckpoint, page: MailboxPage
    ) -> None:
        async with self.sessions() as db, db.begin():
            account = await self._account(db, actor, expected.mailbox_id, lock=True)
            if (
                account.revision != expected.revision
                or account.cursor != expected.cursor
                or account.email != expected.email
            ):
                raise ValidationError("邮箱同步检查点已变化")
            if page.reset:
                account.generation += 1
            for message in page.messages:
                values = {
                    "tenant_id": actor.tenant_id,
                    "mailbox_id": account.mailbox_id,
                    "message_id": message.message_id,
                    "thread_id": message.thread_id,
                    "occurred_at": message.occurred_at,
                    "subject": message.subject,
                    "sender": message.sender,
                    "snippet": message.snippet,
                    "labels": message.labels,
                    "content": message.model_dump(mode="json"),
                    "raw": message.raw,
                    "generation": account.generation,
                }
                statement = insert(Message).values(**values)
                await db.execute(
                    statement.on_conflict_do_update(
                        index_elements=["tenant_id", "mailbox_id", "message_id"],
                        set_={
                            k: v
                            for k, v in values.items()
                            if k not in {"tenant_id", "mailbox_id", "message_id"}
                        },
                        where=(Message.tenant_id == actor.tenant_id)
                        & (Message.mailbox_id == account.mailbox_id),
                    )
                )
            if page.deleted_ids:
                await db.execute(
                    delete(Message).where(
                        Message.tenant_id == actor.tenant_id,
                        Message.mailbox_id == account.mailbox_id,
                        Message.message_id.in_(page.deleted_ids),
                    )
                )
            if page.full_scan_complete:
                await db.execute(
                    delete(Message).where(
                        Message.tenant_id == actor.tenant_id,
                        Message.mailbox_id == account.mailbox_id,
                        Message.generation != account.generation,
                    )
                )
            account.cursor, account.phase = page.cursor, page.phase
            account.revision += 1
            account.failure_code = None
            account.last_attempt_at = datetime.now(UTC)
            if page.phase == "synced":
                account.last_synced_at = account.last_attempt_at
            if page.phase == "synced" or page.refresh_complete:
                account.sync_requested = False

    async def failed(self, actor: MailboxActor, mailbox_id: str, code: str) -> None:
        async with self.sessions() as db, db.begin():
            row = await self._account(db, actor, mailbox_id, lock=True)
            row.failure_code = MailboxFailure(code).code
            row.last_attempt_at = datetime.now(UTC)

    async def request_sync(self, actor: MailboxActor, mailbox_id: str) -> None:
        async with self.sessions() as db, db.begin():
            row = await self._account(db, actor, mailbox_id, lock=True)
            row.sync_requested = True

    async def threads(
        self,
        actor: MailboxActor,
        mailbox_id: str,
        *,
        search: str,
        label: str | None,
        offset: int,
        limit: int,
    ) -> MailThreadPage:
        async with self.sessions() as db, db.begin():
            await self._account(db, actor, mailbox_id)
            scoped = [
                Message.tenant_id == actor.tenant_id,
                Message.mailbox_id == mailbox_id,
            ]
            filtered = list(scoped)
            if search:
                pattern = (
                    "%"
                    + search.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                    + "%"
                )
                filtered.append(
                    or_(
                        Message.subject.ilike(pattern, escape="\\"),
                        Message.sender.ilike(pattern, escape="\\"),
                        Message.content["body_text"].astext.ilike(pattern, escape="\\"),
                    )
                )
            if label == "ARCHIVED":
                filtered.extend(
                    ~Message.labels.contains([value])
                    for value in ("INBOX", "SPAM", "TRASH", "DRAFT")
                )
            elif label:
                filtered.append(Message.labels.contains([label]))
            matching = select(Message.thread_id).where(*filtered).distinct()
            grouped = (
                select(
                    Message.thread_id,
                    func.max(Message.occurred_at).label("latest_at"),
                    func.count().label("message_count"),
                )
                .where(*scoped, Message.thread_id.in_(matching))
                .group_by(Message.thread_id)
                .order_by(
                    func.max(Message.occurred_at).desc(), Message.thread_id.desc()
                )
                .offset(offset)
                .limit(limit + 1)
            )
            rows = (await db.execute(grouped)).all()
            items = []
            for row in rows[:limit]:
                latest = (
                    await db.scalars(
                        select(Message)
                        .where(*scoped, Message.thread_id == row.thread_id)
                        .order_by(Message.occurred_at.desc(), Message.message_id.desc())
                        .limit(1)
                    )
                ).one()
                unread = await db.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        *scoped,
                        Message.thread_id == row.thread_id,
                        Message.labels.contains(["UNREAD"]),
                    )
                )
                items.append(
                    MailThreadView(
                        thread_id=row.thread_id,
                        subject=latest.subject,
                        sender=latest.sender,
                        snippet=latest.snippet,
                        latest_at=row.latest_at,
                        message_count=row.message_count,
                        unread=bool(unread),
                    )
                )
            return MailThreadPage(
                items=items, next_offset=offset + limit if len(rows) > limit else None
            )

    async def messages(
        self,
        actor: MailboxActor,
        mailbox_id: str,
        thread_id: str,
        *,
        offset: int,
        limit: int,
    ) -> MailMessagePage:
        async with self.sessions() as db, db.begin():
            account = await self._account(db, actor, mailbox_id)
            rows = (
                await db.scalars(
                    TenantScopedRepository(actor.tenant_id)
                    .scoped_query(Message)
                    .where(
                        Message.mailbox_id == mailbox_id, Message.thread_id == thread_id
                    )
                    .order_by(Message.occurred_at, Message.message_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            items = [
                MailMessageView(
                    **row.content,
                    source_url=(
                        "https://mail.google.com/mail/u/?authuser="
                        + quote(account.email, safe="")
                        + "#all/"
                        + row.message_id
                    ),
                )
                for row in rows[:limit]
            ]
            return MailMessagePage(
                items=items, next_offset=offset + limit if len(rows) > limit else None
            )
