"""复用现有当前授权锁，原子追加处理历史，绝不修改邮件事实。"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.business_mail_followup import (
    BusinessMailDraftCommand,
    BusinessMailFollowupConflict,
    BusinessMailFollowupEvent,
    BusinessMailFollowupView,
    BusinessMailHandlingCommand,
    BusinessMailReplyDraftView,
    validate_idempotency_key,
)
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.business_mail_followup_tables import (
    BusinessMailFollowupEventRow,
    BusinessMailFollowupRow,
    BusinessMailReplyDraftRow,
)
from infra.db.business_mail_tables import BusinessMailboxRow, BusinessMailRow
from shared.errors import ValidationError
from shared.schemas.identifiers import new_id


class CurrentBusinessMailLease:
    """最窄适配：沿用旧仓储员工→邮箱→源消息锁与绑定检查。"""

    def __init__(self, source: SqlBusinessMailRepository) -> None:
        self.source = source

    async def acquire(
        self, db: AsyncSession, actor: BusinessMailActor, message_id: str
    ) -> tuple[BusinessMailRow, BusinessMailboxRow]:
        return await self.source._message(db, actor, message_id)

    async def check(self, actor: BusinessMailActor, box: BusinessMailboxRow) -> None:
        await self.source.binding.check(actor, box.address, box.binding_operation_id)


class SqlBusinessMailFollowupRepository:
    def __init__(self, source: SqlBusinessMailRepository) -> None:
        self.sessions = source.sessions
        self.lease = CurrentBusinessMailLease(source)

    async def _view(
        self,
        db: AsyncSession,
        actor: BusinessMailActor,
        message: BusinessMailRow,
        box: BusinessMailboxRow,
    ) -> BusinessMailFollowupView:
        current = await db.scalar(
            select(BusinessMailFollowupRow)
            .where(
                BusinessMailFollowupRow.tenant_id == actor.tenant_id,
                BusinessMailFollowupRow.message_id == message.message_id,
            )
            .with_for_update()
        )
        history = (
            await db.scalars(
                select(BusinessMailFollowupEventRow)
                .where(
                    BusinessMailFollowupEventRow.tenant_id == actor.tenant_id,
                    BusinessMailFollowupEventRow.message_id == message.message_id,
                )
                .order_by(BusinessMailFollowupEventRow.revision)
            )
        ).all()
        draft = None
        if current and current.draft_id:
            draft = await db.scalar(
                select(BusinessMailReplyDraftRow).where(
                    BusinessMailReplyDraftRow.tenant_id == actor.tenant_id,
                    BusinessMailReplyDraftRow.message_id == message.message_id,
                    BusinessMailReplyDraftRow.draft_id == current.draft_id,
                )
            )
        return BusinessMailFollowupView.model_validate({
            "message_id": message.message_id,
            "revision": current.revision if current else 0,
            "owner_id": box.owner_id,
            "status": current.status if current else "pending",
            "draft": BusinessMailReplyDraftView(
                **{
                    k: getattr(draft, k)
                    for k in BusinessMailReplyDraftView.model_fields
                }
            )
            if draft
            else None,
            "history": [
                BusinessMailFollowupEvent(
                    **{k: getattr(h, k) for k in BusinessMailFollowupEvent.model_fields}
                )
                for h in history
            ],
        })

    async def get(
        self, actor: BusinessMailActor, message_id: str
    ) -> BusinessMailFollowupView:
        async with self.sessions.begin() as db:
            message, box = await self.lease.acquire(db, actor, message_id)
            result = await self._view(db, actor, message, box)
            await self.lease.check(actor, box)
            return result

    async def apply(
        self,
        actor: BusinessMailActor,
        message_id: str,
        command: BusinessMailDraftCommand | BusinessMailHandlingCommand,
        *,
        idempotency_key: str,
    ) -> BusinessMailFollowupView:
        key = validate_idempotency_key(idempotency_key)
        kind = (
            "draft_saved"
            if isinstance(command, BusinessMailDraftCommand)
            else "handling_updated"
        )
        async with self.sessions.begin() as db:
            message, box = await self.lease.acquire(db, actor, message_id)
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "endpoint": kind,
                        "command": command.model_dump(),
                        "actor": actor.model_dump(),
                        "message_id": message_id,
                        "source_sha256": message.source_sha256,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            # 跨消息请求键串行，避免相同键并发创建两个业务动作。
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {
                    "lock_key": int.from_bytes(
                        hashlib.sha256((str(actor.tenant_id) + key).encode()).digest()[
                            :8
                        ],
                        "big",
                        signed=True,
                    )
                },
            )
            current = await self._view(db, actor, message, box)
            previous = await db.scalar(
                select(BusinessMailFollowupEventRow).where(
                    BusinessMailFollowupEventRow.tenant_id == actor.tenant_id,
                    BusinessMailFollowupEventRow.request_key == key,
                )
            )
            if previous:
                if previous.payload_sha256 != digest:
                    raise BusinessMailFollowupConflict()
                await self.lease.check(actor, box)
                return current
            if current.revision != command.expected_revision:
                raise BusinessMailFollowupConflict()
            draft_id = None
            now = datetime.now(UTC)
            if isinstance(command, BusinessMailDraftCommand):
                if message.source_kind != "full_mime":
                    raise ValidationError("邮件原文不完整，不能保存回复草稿")
                draft_id = new_id("bmd")
                db.add(
                    BusinessMailReplyDraftRow(
                        tenant_id=actor.tenant_id,
                        message_id=message_id,
                        draft_id=draft_id,
                        subject=command.subject,
                        body_text=command.body_text,
                        source_sha256=message.source_sha256,
                        created_by=actor.employee_id,
                        created_at=now,
                    )
                )
            state = await db.scalar(
                select(BusinessMailFollowupRow)
                .where(
                    BusinessMailFollowupRow.tenant_id == actor.tenant_id,
                    BusinessMailFollowupRow.message_id == message_id,
                )
                .with_for_update()
            )
            if state is None:
                state = BusinessMailFollowupRow(
                    tenant_id=actor.tenant_id,
                    message_id=message_id,
                    revision=0,
                    status="pending",
                    draft_id=None,
                )
                db.add(state)
            state.revision += 1
            if draft_id:
                state.draft_id = draft_id
            elif isinstance(command, BusinessMailHandlingCommand):
                state.status = command.status
            db.add(
                BusinessMailFollowupEventRow(
                    tenant_id=actor.tenant_id,
                    message_id=message_id,
                    event_id=new_id("bme"),
                    kind=kind,
                    revision=state.revision,
                    status=state.status,
                    note="保存未发送回复草稿" if isinstance(command, BusinessMailDraftCommand) else command.note,
                    actor_id=actor.employee_id,
                    created_at=now,
                    draft_id=draft_id,
                    request_key=key,
                    payload_sha256=digest,
                )
            )
            await db.flush()
            await self.lease.check(actor, box)
            return await self._view(db, actor, message, box)
