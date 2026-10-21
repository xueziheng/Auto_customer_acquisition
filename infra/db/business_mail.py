"""普通来信仓储：当前员工→邮箱锁；整页原子提交，分类先claim后独立完成。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.business_mail import (
    BusinessMailActor,
    BusinessMailClassification,
    BusinessMailConflict,
    BusinessMailCorrection,
    BusinessMailDetail,
    BusinessMailItem,
    BusinessMailPage,
    BusinessMailResult,
    BusinessMailSource,
    MailBucket,
)
from infra.db.business_mail_tables import (
    BusinessMailboxRow,
    BusinessMailClassificationRow,
    BusinessMailReceiptRow,
    BusinessMailRow,
)
from infra.db.dialogue_execution_tables import DialogueExecutionRow
from infra.db.tables import (
    EmployeeRow,
    ModelInvocationRow,
    OutreachMessageAttemptRow,
    RawArtifactRow,
    ToolCallEventRow,
    ToolCallRow,
)
from shared.business_mail import BusinessArchivedPage
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import InvocationIdentity, ModelLimits
from tool_gateway.model_usage import window_start


class BusinessMailboxBinding(Protocol):
    async def check(
        self, actor: BusinessMailActor, address: str, operation: str
    ) -> None:
        """只在受信适配器重读绑定版本；不向模型交付凭证。"""
        ...


class BusinessCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    mailbox_id: str
    uidvalidity: int | None
    after_uid: int
    revision: int


class BusinessClaim(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    message_id: str
    mailbox_id: str
    run_id: str
    revision: int
    sequence: int = Field(default=0, ge=0)
    subject: str = Field(repr=False, exclude=True)
    body_text: str = Field(repr=False, exclude=True)
    model: str
    configuration_version: str


class SqlBusinessMailRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        binding: BusinessMailboxBinding,
    ) -> None:
        self.sessions, self.binding = sessions, binding

    async def _employee(self, db: AsyncSession, actor: BusinessMailActor) -> None:
        found = await db.scalar(
            select(EmployeeRow.employee_id)
            .where(
                EmployeeRow.tenant_id == actor.tenant_id,
                EmployeeRow.employee_id == actor.employee_id,
                EmployeeRow.user_id == actor.user_id,
                EmployeeRow.is_active.is_(True),
                EmployeeRow.role.in_(("boss", "manager", "sales")),
            )
            .with_for_update(key_share=True)
        )
        if found is None:
            raise PermissionDenied("当前员工无权访问业务来信")

    async def _mailbox(
        self, db: AsyncSession, actor: BusinessMailActor, mailbox_id: str
    ) -> BusinessMailboxRow:
        await self._employee(db, actor)
        row = await db.scalar(
            select(BusinessMailboxRow)
            .where(
                BusinessMailboxRow.tenant_id == actor.tenant_id,
                BusinessMailboxRow.mailbox_id == mailbox_id,
                BusinessMailboxRow.owner_id == actor.employee_id,
                BusinessMailboxRow.user_id == actor.user_id,
                BusinessMailboxRow.enabled.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            raise PermissionDenied("当前员工不是此邮箱负责人")
        await self.binding.check(actor, row.address, row.binding_operation_id)
        return row

    async def register(
        self, actor: BusinessMailActor, *, address: str, operation: str
    ) -> str:
        """仅显式worker scope可创建；旧scope不能用重启重写轮换后的授权。"""
        async with self.sessions.begin() as db:
            await self._employee(db, actor)
            await self.binding.check(actor, address, operation)
            existing = await db.scalar(
                select(BusinessMailboxRow)
                .where(
                    BusinessMailboxRow.tenant_id == actor.tenant_id,
                    BusinessMailboxRow.address == address,
                )
                .with_for_update()
            )
            if existing is not None:
                if (
                    existing.owner_id,
                    existing.user_id,
                    existing.binding_operation_id,
                    existing.enabled,
                ) != (actor.employee_id, actor.user_id, operation, True):
                    raise PermissionDenied("普通收信scope已改变，必须明确重新授权")
                return existing.mailbox_id
            row = BusinessMailboxRow(
                tenant_id=actor.tenant_id,
                mailbox_id=new_id("bmb"),
                owner_id=actor.employee_id,
                user_id=actor.user_id,
                address=address,
                binding_operation_id=operation,
                enabled=True,
                uidvalidity=None,
                after_uid=0,
                revision=0,
                created_at=datetime.now(UTC),
            )
            db.add(row)
            return row.mailbox_id

    async def checkpoint(
        self, actor: BusinessMailActor, mailbox_id: str
    ) -> BusinessCheckpoint:
        async with self.sessions.begin() as db:
            row = await self._mailbox(db, actor, mailbox_id)
            return BusinessCheckpoint(
                mailbox_id=mailbox_id,
                uidvalidity=row.uidvalidity,
                after_uid=row.after_uid,
                revision=row.revision,
            )

    async def authorize(self, actor: BusinessMailActor, mailbox_id: str) -> None:
        async with self.sessions.begin() as db:
            await self._mailbox(db, actor, mailbox_id)

    async def apply_page(
        self,
        actor: BusinessMailActor,
        expected: BusinessCheckpoint,
        page: BusinessArchivedPage,
    ) -> None:
        """所有候选和原件关联验证完成才推进整页游标；失败不跳过任何UID。"""
        async with self.sessions.begin() as db:
            box = await self._mailbox(db, actor, expected.mailbox_id)
            if (box.revision, box.uidvalidity, box.after_uid) != (
                expected.revision,
                expected.uidvalidity,
                expected.after_uid,
            ):
                raise BusinessMailConflict()
            if (
                not 0 <= page.after_uid <= page.next_uid <= 4294967295
                or not 1 <= page.uidvalidity <= 4294967295
                or len(page.messages) > 50
                or (
                    page.uidvalidity == expected.uidvalidity
                    and page.after_uid != expected.after_uid
                )
            ):
                raise ValidationError("普通来信页与游标不符")
            uids = tuple(m.uid for m in page.messages)
            if tuple(sorted(set(uids))) != uids:
                raise ValidationError("普通来信页UID重复或乱序")
            for item in page.messages:
                if (
                    item.uidvalidity != page.uidvalidity
                    or not page.after_uid < item.uid <= page.next_uid
                ):
                    raise ValidationError("普通来信候选超出页范围")
                raw = await db.scalar(
                    select(RawArtifactRow.artifact_id).where(
                        RawArtifactRow.tenant_id == actor.tenant_id,
                        RawArtifactRow.artifact_id == item.artifact_id,
                        RawArtifactRow.kind == "email_raw",
                        RawArtifactRow.content_hash == item.source_sha256,
                        RawArtifactRow.mime_type == "message/rfc822",
                    )
                )
                if raw is None:
                    raise ValidationError("普通来信缺少真实原件关联")
                receipt = await db.scalar(
                    select(BusinessMailReceiptRow).where(
                        BusinessMailReceiptRow.tenant_id == actor.tenant_id,
                        BusinessMailReceiptRow.mailbox_id == box.mailbox_id,
                        BusinessMailReceiptRow.uidvalidity == item.uidvalidity,
                        BusinessMailReceiptRow.uid == item.uid,
                    )
                )
                if receipt is not None:
                    if receipt.source_sha256 != item.source_sha256:
                        raise BusinessMailConflict()
                    continue
                existing = await db.scalar(
                    select(BusinessMailRow)
                    .where(
                        BusinessMailRow.tenant_id == actor.tenant_id,
                        BusinessMailRow.mailbox_id == box.mailbox_id,
                        or_(
                            BusinessMailRow.content_sha256 == item.content_sha256,
                            BusinessMailRow.source_sha256 == item.source_sha256,
                        ),
                    )
                    .order_by(BusinessMailRow.ingested_at)
                    .limit(1)
                )
                if existing is None:
                    header = item.in_reply_to or (item.dialogue_feedback.original_message_id if item.dialogue_feedback else None)
                    linked = False
                    if header is not None:
                        linked = (await db.scalar(select(DialogueExecutionRow.source_message_id).where(
                            DialogueExecutionRow.tenant_id == actor.tenant_id,
                            DialogueExecutionRow.deterministic_message_id == header,
                            DialogueExecutionRow.state == 'sent'))) is not None
                        if not linked:
                            linked = (await db.scalar(select(OutreachMessageAttemptRow.attempt_id).where(
                                OutreachMessageAttemptRow.tenant_id == actor.tenant_id,
                                OutreachMessageAttemptRow.deterministic_message_id == header,
                                OutreachMessageAttemptRow.state == 'sent'))) is not None
                    failure = item.parse_failure or ('ordinary_dialogue_pending' if linked else None)
                    existing = BusinessMailRow(
                        tenant_id=actor.tenant_id,
                        message_id=new_id("bmi"),
                        mailbox_id=box.mailbox_id,
                        sender=item.sender,
                        subject=item.subject,
                        body_text=item.body_text,
                        message_header=item.message_header, in_reply_to=item.in_reply_to,
                        reply_recipient=item.reply_recipient,
                        dialogue_feedback=item.dialogue_feedback.model_dump(mode='json') if item.dialogue_feedback else None,
                        received_at=item.received_at,
                        ingested_at=datetime.now(UTC),
                        content_sha256=item.content_sha256,
                        mime_sha256=item.mime_sha256,
                        source_sha256=item.source_sha256,
                        source_kind=item.source_kind,
                        artifact_id=item.artifact_id,
                        uidvalidity=item.uidvalidity,
                        uid=item.uid,
                        category="review" if failure else "pending",
                        classification_status="failed"
                        if failure
                        else "pending",
                        revision=0,
                        failure_code=failure,
                        run_id=new_id("run"),
                    )
                    db.add(existing)
                    await db.flush()
                    if failure:
                        self._history(
                            db,
                            existing,
                            category="review",
                            origin="system",
                            reason="邮件内容需要人工确认",
                            failure=failure,
                        )
                db.add(
                    BusinessMailReceiptRow(
                        tenant_id=actor.tenant_id,
                        mailbox_id=box.mailbox_id,
                        uidvalidity=item.uidvalidity,
                        uid=item.uid,
                        message_id=existing.message_id,
                        mime_sha256=item.mime_sha256,
                        source_sha256=item.source_sha256,
                        source_kind=item.source_kind,
                        artifact_id=item.artifact_id,
                    )
                )
            await self.binding.check(actor, box.address, box.binding_operation_id)
            box.uidvalidity, box.after_uid = page.uidvalidity, page.next_uid
            box.revision += 1
            box.last_synced_at = datetime.now(UTC)
            box.failure_code = None

    async def _message(
        self, db: AsyncSession, actor: BusinessMailActor, message_id: str
    ) -> tuple[BusinessMailRow, BusinessMailboxRow]:
        # 先安全投影mailbox ID；正文只能在当前员工/owner锁核验后读取。
        mailbox = await db.scalar(
            select(BusinessMailRow.mailbox_id).where(
                BusinessMailRow.tenant_id == actor.tenant_id,
                BusinessMailRow.message_id == message_id,
            )
        )
        if mailbox is None:
            raise PermissionDenied("业务来信不存在或无权访问")
        box = await self._mailbox(db, actor, mailbox)
        row = await db.scalar(
            select(BusinessMailRow)
            .where(
                BusinessMailRow.tenant_id == actor.tenant_id,
                BusinessMailRow.message_id == message_id,
            )
            .with_for_update()
        )
        if row is None:
            raise PermissionDenied("业务来信不存在或无权访问")
        return row, box

    def _item(self, row: BusinessMailRow, box: BusinessMailboxRow) -> BusinessMailItem:
        return BusinessMailItem.model_validate({
            "message_id": row.message_id,
            "mailbox_id": row.mailbox_id,
            "mailbox_address": box.address,
            "owner_id": box.owner_id,
            "sender": row.sender,
            "subject": row.subject,
            "snippet": row.body_text[:240],
            "received_at": row.received_at,
            "category": row.category,
            "classification_status": row.classification_status,
            "revision": row.revision,
            "failure_code": row.failure_code,
        })

    async def list(
        self, actor: BusinessMailActor, *, bucket: MailBucket, offset: int, limit: int
    ) -> BusinessMailPage:
        async with self.sessions.begin() as db:
            await self._employee(db, actor)
            boxes = (
                await db.scalars(
                    select(BusinessMailboxRow)
                    .where(
                        BusinessMailboxRow.tenant_id == actor.tenant_id,
                        BusinessMailboxRow.owner_id == actor.employee_id,
                        BusinessMailboxRow.user_id == actor.user_id,
                        BusinessMailboxRow.enabled.is_(True),
                    )
                    .order_by(BusinessMailboxRow.mailbox_id)
                    .with_for_update()
                )
            ).all()
            for box in boxes:
                await self.binding.check(actor, box.address, box.binding_operation_id)
            ids = {b.mailbox_id: b for b in boxes}
            query = select(BusinessMailRow).where(
                BusinessMailRow.tenant_id == actor.tenant_id,
                BusinessMailRow.mailbox_id.in_(ids),
            )
            if bucket != "all":
                query = query.where(BusinessMailRow.category == bucket)
            rows = (
                await db.scalars(
                    query.order_by(
                        BusinessMailRow.received_at.desc(),
                        BusinessMailRow.message_id.desc(),
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            result = BusinessMailPage(
                items=[self._item(r, ids[r.mailbox_id]) for r in rows[:limit]],
                next_offset=offset + limit if len(rows) > limit else None,
            )
            for box in boxes:
                await self.binding.check(actor, box.address, box.binding_operation_id)
            return result

    async def _detail(
        self, db: AsyncSession, row: BusinessMailRow, box: BusinessMailboxRow
    ) -> BusinessMailDetail:
        history = (
            await db.scalars(
                select(BusinessMailClassificationRow)
                .where(
                    BusinessMailClassificationRow.tenant_id == row.tenant_id,
                    BusinessMailClassificationRow.message_id == row.message_id,
                )
                .order_by(BusinessMailClassificationRow.revision)
            )
        ).all()
        return BusinessMailDetail(
            **self._item(row, box).model_dump(),
            body_text=row.body_text,
            source=BusinessMailSource.model_validate({
                "uidvalidity": row.uidvalidity,
                "uid": row.uid,
                "mime_sha256": row.mime_sha256,
                "source_sha256": row.source_sha256,
                "source_kind": row.source_kind,
                "artifact_id": row.artifact_id,
                "received_at": row.received_at,
            }),
            classification_history=[
                BusinessMailClassification(
                    **{
                        k: getattr(h, k)
                        for k in BusinessMailClassification.model_fields
                    }
                )
                for h in history
            ],
        )

    async def detail(
        self, actor: BusinessMailActor, message_id: str
    ) -> BusinessMailDetail:
        async with self.sessions.begin() as db:
            row, box = await self._message(db, actor, message_id)
            result = await self._detail(db, row, box)
            await self.binding.check(actor, box.address, box.binding_operation_id)
            return result

    def _history(
        self,
        db: AsyncSession,
        row: BusinessMailRow,
        *,
        category: str,
        origin: str,
        reason: str,
        quote: str | None = None,
        actor_id: str | None = None,
        failure: str | None = None,
    ) -> None:
        row.revision += 1
        db.add(
            BusinessMailClassificationRow(
                tenant_id=row.tenant_id,
                classification_id=new_id("bmc"),
                message_id=row.message_id,
                revision=row.revision,
                category=category,
                origin=origin,
                reason=reason,
                evidence_quote=quote,
                model=row.model if origin == "model" else None,
                configuration_version=row.configuration_version
                if origin == "model"
                else None,
                actor_id=actor_id,
                created_at=datetime.now(UTC),
            )
        )

    async def correct(
        self, actor: BusinessMailActor, message_id: str, command: BusinessMailCorrection
    ) -> BusinessMailDetail:
        async with self.sessions.begin() as db:
            row, box = await self._message(db, actor, message_id)
            if row.revision != command.expected_revision:
                raise BusinessMailConflict()
            self._history(
                db,
                row,
                category=command.category,
                origin="human",
                reason=command.note,
                actor_id=actor.employee_id,
            )
            row.category, row.classification_status, row.failure_code = (
                command.category,
                "classified",
                None,
            )
            await db.flush()
            await self.binding.check(actor, box.address, box.binding_operation_id)
            return await self._detail(db, row, box)

    async def claim_next(
        self, actor: BusinessMailActor, mailbox_id: str, *, model: str, version: str
    ) -> BusinessClaim | None:
        async with self.sessions.begin() as db:
            box = await self._mailbox(db, actor, mailbox_id)
            if (
                box.model_blocked_until is not None
                and box.model_blocked_until > datetime.now(UTC)
            ):
                return None
            row = await db.scalar(
                select(BusinessMailRow)
                .where(
                    BusinessMailRow.tenant_id == actor.tenant_id,
                    BusinessMailRow.mailbox_id == mailbox_id,
                    BusinessMailRow.classification_status == "pending",
                    BusinessMailRow.claimed_at.is_(None),
                )
                .order_by(
                    BusinessMailRow.received_at.desc(),
                    BusinessMailRow.message_id.desc(),
                )
                .limit(1)
                .with_for_update()
            )
            if row is None:
                return None
            if row.model is not None and (
                row.model != model or row.configuration_version != version
            ):
                raise PermissionDenied("原普通来信准入身份不能更换模型或配置")
            sequence = await self._admission_sequence(db, actor, row)
            (
                row.classification_status,
                row.claimed_at,
                row.model,
                row.configuration_version,
            ) = "classifying", datetime.now(UTC), model, version
            return BusinessClaim(
                message_id=row.message_id,
                mailbox_id=mailbox_id,
                run_id=row.run_id,
                revision=row.revision,
                sequence=sequence,
                subject=row.subject,
                body_text=row.body_text,
                model=model,
                configuration_version=version,
            )

    async def validate_claim(
        self,
        actor: BusinessMailActor,
        run_id: str,
        model: str,
        version: str,
        *,
        allow_corrected: bool = False,
        sequence: int = 0,
    ) -> None:
        async with self.sessions.begin() as db:
            message_id = await db.scalar(
                select(BusinessMailRow.message_id).where(
                    BusinessMailRow.tenant_id == actor.tenant_id,
                    BusinessMailRow.run_id == run_id,
                )
            )
            if message_id is None:
                raise PermissionDenied("普通来信分类身份无效")
            row, _ = await self._message(db, actor, message_id)
            if (
                sequence != await self._admission_sequence(db, actor, row)
                or row.claimed_at is None
                or row.model != model
                or row.configuration_version != version
                or row.classification_status == "failed"
                or (row.classification_status != "classifying" and not allow_corrected)
            ):
                raise PermissionDenied("普通来信分类身份已失效")

    async def finish(
        self,
        actor: BusinessMailActor,
        claim: BusinessClaim,
        *,
        result: BusinessMailResult | None,
        failure: str | None,
        model_blocked_until: datetime | None = None,
    ) -> None:
        async with self.sessions.begin() as db:
            row, box = await self._message(db, actor, claim.message_id)
            if (
                row.run_id,
                row.model,
                row.configuration_version,
                row.claimed_at is not None,
            ) != (claim.run_id, claim.model, claim.configuration_version, True):
                raise BusinessMailConflict()
            prior = await db.scalar(
                select(BusinessMailClassificationRow.classification_id)
                .where(
                    BusinessMailClassificationRow.tenant_id == actor.tenant_id,
                    BusinessMailClassificationRow.message_id == row.message_id,
                    BusinessMailClassificationRow.origin.in_(("model", "system")),
                )
                .limit(1)
            )
            if prior is not None:
                return
            if failure == "quota" and model_blocked_until is not None:
                if model_blocked_until <= datetime.now(UTC):
                    raise ValidationError("配额暂停时间必须有效")
                box.model_blocked_until = model_blocked_until
            category = result.category if result else "review"
            still_original = row.revision == claim.revision
            self._history(
                db,
                row,
                category=category,
                origin="model" if result else "system",
                reason=result.reason if result else "自动识别未完成，请人工确认",
                quote=result.evidence_quote if result else None,
            )
            if still_original:
                row.category = category
                row.classification_status = "classified" if result else "failed"
                row.failure_code = failure
            await self.binding.check(actor, box.address, box.binding_operation_id)

    async def recover_claims(self, actor: BusinessMailActor, mailbox_id: str) -> int:
        """仅持单实例锁启动后调用；未知执行永久留待确认，不自动再次调用模型。"""
        async with self.sessions.begin() as db:
            await self._mailbox(db, actor, mailbox_id)
            rows = (
                await db.scalars(
                    select(BusinessMailRow)
                    .where(
                        BusinessMailRow.tenant_id == actor.tenant_id,
                        BusinessMailRow.mailbox_id == mailbox_id,
                        BusinessMailRow.classification_status == "classifying",
                    )
                    .with_for_update()
                )
            ).all()
            for row in rows:
                self._history(
                    db,
                    row,
                    category="review",
                    origin="system",
                    reason="上次识别结果未知，请人工确认",
                )
                row.category, row.classification_status, row.failure_code = (
                    "review",
                    "failed",
                    "unknown",
                )
            return len(rows)

    async def model_availability(
        self, actor: BusinessMailActor, mailbox_id: str, limits: ModelLimits
    ) -> tuple[str, datetime | None]:
        """仅预览共享账本以避免无意义claim；最终原子准入始终由model.generate完成。"""
        async with self.sessions.begin() as db:
            box = await self._mailbox(db, actor, mailbox_id)
            stamp = datetime.now(UTC)
            start = window_start(stamp, limits.window_seconds)
            base = select(ModelInvocationRow).where(
                ModelInvocationRow.tenant_id == actor.tenant_id,
                ModelInvocationRow.state != "rejected",
            )
            recent = base.where(ModelInvocationRow.created_at >= start).subquery()
            active = base.where(ModelInvocationRow.slot_released.is_(False)).subquery()
            total = int(await db.scalar(select(func.count()).select_from(recent)) or 0)
            personal = int(
                await db.scalar(
                    select(func.count())
                    .select_from(recent)
                    .where(recent.c.employee_id == actor.employee_id)
                )
                or 0
            )
            if (
                (limits.tenant_calls is not None and total >= limits.tenant_calls)
                or (limits.employee_calls is not None and personal >= limits.employee_calls)
            ):
                until = start + timedelta(seconds=limits.window_seconds)
                box.model_blocked_until = until
                return "window", until
            # 当前配置和账本已允许时取消旧窗口暂停，不改写历史调用。
            box.model_blocked_until = None
            active_total = int(
                await db.scalar(select(func.count()).select_from(active)) or 0
            )
            active_personal = int(
                await db.scalar(
                    select(func.count())
                    .select_from(active)
                    .where(active.c.employee_id == actor.employee_id)
                )
                or 0
            )
            if (
                active_total >= limits.tenant_concurrency
                or active_personal >= limits.employee_concurrency
            ):
                return "concurrency", None
            return "ready", None

    def _model_key(
        self, actor: BusinessMailActor, row: BusinessMailRow, sequence: int
    ) -> str:
        """与既有model.generate规范身份键一致；不含正文、凭证或可变模型判断。"""
        identity = InvocationIdentity.model_validate(dict(
            **actor.model_dump(),
            run_id=row.run_id,
            capability="business_mail_triage",
            configuration_version=row.configuration_version,
            sequence=sequence,
        ))
        return (
            "model:" + hashlib.sha256(identity.model_dump_json().encode()).hexdigest()
        )

    async def _admission_sequence(
        self, db: AsyncSession, actor: BusinessMailActor, row: BusinessMailRow
    ) -> int:
        """完整的零预留quota拒绝链派生下一身份；旧canonical及事件只读，不复用失败键。"""
        calls = (
            await db.scalars(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == actor.tenant_id,
                    ToolCallRow.run_id == row.run_id,
                    ToolCallRow.tool_id == "model.generate",
                    ToolCallRow.idempotency_key.is_not(None),
                    ToolCallRow.error_category == "rate_limited",
                )
            )
        ).all()
        if not calls:
            return 0
        keyed = {call.idempotency_key: call for call in calls}
        for sequence in range(len(calls)):
            call = keyed.get(self._model_key(actor, row, sequence))
            if (
                call is None
                or call.user_id != actor.user_id
                or call.tool_version != "v1"
                or call.status != "failed_transient"
                or call.provider_ref is not None
                or call.attempt_count != 1
            ):
                raise PermissionDenied("普通来信准入拒绝链不完整")
            event = await db.scalar(
                select(ToolCallEventRow.event_id)
                .where(
                    ToolCallEventRow.tenant_id == actor.tenant_id,
                    ToolCallEventRow.tool_call_id == call.tool_call_id,
                    ToolCallEventRow.run_id == row.run_id,
                    ToolCallEventRow.actor_id == actor.user_id,
                    ToolCallEventRow.stage == "rate_limit",
                    ToolCallEventRow.outcome == "failed",
                    ToolCallEventRow.category == "rate_limited",
                )
                .limit(1)
            )
            if event is None:
                raise PermissionDenied("普通来信未派发证据缺失")
        return len(calls)

    async def defer_unattempted_quota(
        self, actor: BusinessMailActor, claim: BusinessClaim
    ) -> bool:
        """只有精确rate_limit拒绝且整个run无任何预留记录，才能恢复pending。

        rejected也是真实调用历史，和reserved/dispatched/unknown一样不得走本入口。
        下一序号从完整canonical拒绝链推导，旧键在授权阶段失效；不修改旧工具账本。
        """
        async with self.sessions.begin() as db:
            row, box = await self._message(db, actor, claim.message_id)
            if (
                row.run_id,
                row.model,
                row.configuration_version,
                row.revision,
                row.classification_status,
            ) != (
                claim.run_id,
                claim.model,
                claim.configuration_version,
                claim.revision,
                "classifying",
            ):
                return False
            invocation = await db.scalar(
                select(ModelInvocationRow.invocation_id)
                .where(
                    ModelInvocationRow.tenant_id == actor.tenant_id,
                    ModelInvocationRow.run_id == claim.run_id,
                )
                .limit(1)
            )
            if invocation is not None:
                return False
            try:
                sequence = await self._admission_sequence(db, actor, row)
            except PermissionDenied:
                return False
            if sequence != claim.sequence + 1:
                return False
            # model/config保留原值，使下一次准入不能换模型或改配置绕过历史拒绝。
            (
                row.classification_status,
                row.category,
                row.claimed_at,
                row.failure_code,
            ) = "pending", "pending", None, None
            await self.binding.check(actor, box.address, box.binding_operation_id)
            return True
