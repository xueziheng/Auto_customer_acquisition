"""入站页唯一session/commit，域UoW仅绑定仓储；取消保留primary。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.conversations.repository import ConversationsUnitOfWork
from domains.conversations.service_impl import ConversationServiceImpl
from domains.outreach.repository import OutreachUnitOfWork
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.email_feedback_uow import (
    AuditSink,
    OutreachServiceBuilder,
    _BoundFactory,
    _BoundOutreachUnitOfWork,
    _TransactionAwareAudit,
)
from infra.db.outbox import PostgresEventBus
from infra.db.repositories.conversations import (
    ClassificationRepositoryImpl,
    ConversationRepositoryImpl,
    MessageRepositoryImpl,
    ReplyWorkRepositoryImpl,
)
from infra.db.repositories.email_inbound import InboundStore, cursor_fact, lock_tenant
from infra.db.tables import EmailInboundReceiptRow, EmailInboundReviewRow
from shared.schemas.email_inbound import ArchivedInboundItem
from shared.schemas.identifiers import MessageId, TenantId, new_id
from workflows.reply_qualification.inbound_contracts import (
    InboundCommitUnknown,
    InboundCursor,
    InboundPageError,
)

logger = logging.getLogger(__name__)


class _BoundConversations(SqlAlchemyConversationsUnitOfWork):
    def __init__(
        self, session: AsyncSession, tenant: TenantId, now: Callable[[], datetime]
    ):
        self._session, self._tenant_id, self._now = session, tenant, now
        self.classifications = ClassificationRepositoryImpl(session, tenant)
        self.conversations = ConversationRepositoryImpl(session, tenant)
        self.messages = MessageRepositoryImpl(session, tenant)
        self.reply_work = ReplyWorkRepositoryImpl(session, tenant)
        self.bus = PostgresEventBus(session, tenant, now=now)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        return None


class SqlAlchemyInboundPageUnitOfWork:
    def __init__(
        self,
        store: InboundStore,
        expected: InboundCursor,
        *,
        outreach_builder: OutreachServiceBuilder,
        audit_sink: AuditSink,
    ):
        self._store, self._expected, self._builder, self._sink = (
            store,
            expected,
            outreach_builder,
            audit_sink,
        )

    async def __aenter__(self) -> Self:
        session = self._store.factory()
        self._session = session
        self._audit = _TransactionAwareAudit(self._sink)
        try:
            tenant = self._store.tenant_id
            await lock_tenant(session, tenant)
            row = await self._store.row(session, lock=True)
            if row is None:
                raise InboundPageError("cursor_conflict")
            self._row = row
            self.current = cursor_fact(row)
            self.conversations = ConversationServiceImpl(
                _BoundFactory[ConversationsUnitOfWork](
                    tenant,
                    lambda t: cast(
                        ConversationsUnitOfWork,
                        _BoundConversations(session, t, self._store.now),
                    ),
                ),
                now=self._store.now,
            )
            self.outreach = self._builder(
                _BoundFactory[OutreachUnitOfWork](
                    tenant,
                    lambda t: cast(
                        OutreachUnitOfWork,
                        _BoundOutreachUnitOfWork(session, t, self._store.now),
                    ),
                ),
                self._audit,
            )
            return self
        except BaseException:
            try:
                await session.rollback()
            except BaseException:  # noqa: BLE001 - 保留原业务/取消异常
                logger.error("入站打开事务回滚失败")
            try:
                await session.close()
            except BaseException:  # noqa: BLE001 - 保留原业务/取消异常
                logger.error("入站打开事务关闭失败")
            raise

    async def receipt(self, digest: str) -> str | None:
        return await self._session.scalar(
            select(EmailInboundReceiptRow.item_fingerprint).where(
                EmailInboundReceiptRow.tenant_id == self._store.tenant_id,
                EmailInboundReceiptRow.mailbox_alias == self._store.mailbox_alias,
                EmailInboundReceiptRow.provider_ref_digest == digest,
            )
        )

    async def record(
        self,
        item: ArchivedInboundItem,
        fingerprint: str,
        message_id: MessageId | None,
        reason: str | None,
    ) -> None:
        raw = item.raw
        self._session.add(
            EmailInboundReceiptRow(
                tenant_id=self._store.tenant_id,
                mailbox_alias=self._store.mailbox_alias,
                provider_ref_digest=item.provider_ref_digest,
                item_fingerprint=fingerprint,
                parser_version=item.parser_version,
                guard_version="credential-marker-v1",
                disposition=item.disposition.value,
                raw_artifact_id=raw.artifact_id if raw else None,
                raw_hash=raw.content_hash if raw else None,
                raw_size=raw.size_bytes if raw else None,
                message_id=message_id,
                created_at=self._store.now(),
            )
        )
        await self._session.flush()
        if reason is not None:
            self._session.add(
                EmailInboundReviewRow(
                    tenant_id=self._store.tenant_id,
                    review_id=new_id("irv"),
                    mailbox_alias=self._store.mailbox_alias,
                    provider_ref_digest=item.provider_ref_digest,
                    raw_artifact_id=raw.artifact_id if raw else None,
                    reason=reason,
                    created_at=self._store.now(),
                )
            )
            await self._session.flush()

    async def advance(self, next_cursor: str) -> None:
        self._row.provider_cursor = next_cursor
        self._row.version += 1
        self._row.last_succeeded_at = self._store.now()
        self._row.blocked_reason = None
        self._row.next_retry_at = None
        await self._session.flush()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        primary = exc is not None
        try:
            if primary:
                await self._session.rollback()
            else:
                try:
                    await self._session.commit()
                except Exception:  # noqa: BLE001 - 提交结果必须由新事务核实
                    raise InboundCommitUnknown() from None
                for record in self._audit.records:
                    try:
                        self._sink.log(
                            actor=record.actor,
                            action=record.action,
                            tenant_id=record.tenant_id,
                            scope=record.scope,
                            rule=record.rule,
                        )
                    except Exception:  # noqa: BLE001 - 独立日志sink失败不重做已提交页
                        logger.error("入站提交后审计刷新失败")
        except BaseException:
            if not primary:
                raise
            logger.error("入站事务清理失败")
        finally:
            self._audit.records.clear()
            try:
                await self._session.close()
            except BaseException:  # noqa: BLE001 - 固定错误并保留primary
                if not primary:
                    raise InboundCommitUnknown() from None
                logger.error("入站事务关闭失败")
