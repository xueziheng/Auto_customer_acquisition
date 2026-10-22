"""普通询盘在0086追加历史上的持久编排事实；不新增业务SQL写入旁路。"""

from datetime import UTC, datetime
from email.utils import getaddresses
from types import SimpleNamespace

from sqlalchemy import exists, or_, select

from domains.conversations.business_inquiry import (
    InquiryAutomationScope,
    InquiryReplyScope,
)
from domains.conversations.business_mail_followup import (
    BusinessMailFollowupService,
    BusinessMailHandlingCommand,
)
from infra.db.business_mail_followup import SqlBusinessMailFollowupRepository
from infra.db.business_mail_tables import BusinessMailClassificationRow, BusinessMailRow
from infra.db.business_reply import CurrentBusinessReplyPolicy
from infra.db.dialogue_execution_tables import DialogueBindingRow
from infra.pilot.config import private_read
from shared.errors import PermissionDenied, ValidationError


class InquiryRecords:
    def __init__(self, source, scope_path, scope):
        self.source, self.path, self.scope = source, scope_path, scope
        self.followup = BusinessMailFollowupService(
            SqlBusinessMailFollowupRepository(source)
        )

    async def authorize(self):
        if (
            InquiryAutomationScope.model_validate_json(private_read(self.path))
            != self.scope
            or not self.scope.approved_at <= datetime.now(UTC) < self.scope.expires_at
        ):
            raise PermissionDenied("普通询盘自动跟进授权已失效")
        await self.source.authorize(self.scope.actor, self.scope.mailbox_id)

    async def list_messages(self):
        await self.authorize()
        s = self.scope
        async with self.source.sessions() as db:
            rows = (
                await db.scalars(
                    select(BusinessMailRow)
                    .where(
                        BusinessMailRow.tenant_id == s.actor.tenant_id,
                        BusinessMailRow.mailbox_id == s.mailbox_id,
                        or_(
                            BusinessMailRow.ingested_at >= s.approved_at,
                            BusinessMailRow.message_id.in_(s.recovery_message_ids),
                        ),
                        BusinessMailRow.source_kind == "full_mime",
                        BusinessMailRow.classification_status == "classified",
                        BusinessMailRow.category == "business",
                        ~exists(select(DialogueBindingRow.binding_id).where(
                            DialogueBindingRow.tenant_id == s.actor.tenant_id,
                            DialogueBindingRow.business_message_id == BusinessMailRow.message_id)),
                    )
                    .order_by(BusinessMailRow.ingested_at, BusinessMailRow.message_id)
                )
            ).all()
        recipients = {x.casefold() for x in s.allowed_recipients}
        return [
            r.message_id
            for r in rows
            if len(getaddresses([r.sender])) == 1
            and getaddresses([r.sender])[0][1].casefold() in recipients
        ]

    async def view(self, original):
        await self.authorize()
        return await self.followup.get(self.scope.actor, original)

    async def event(self, original, key):
        view = await self.view(original)
        return next((e for e in view.history if e.request_key == key), None)

    async def append(self, original, key, status, note):
        current = await self.view(original)
        prior = next((e for e in current.history if e.request_key == key), None)
        if prior:
            if prior.note != note:
                raise ValidationError("同请求已有不同处理记录")
            return current
        if current.status == "completed":
            raise PermissionDenied("负责人已完成处理，系统不覆盖人工状态")
        return await self.followup.handle(
            self.scope.actor,
            original,
            BusinessMailHandlingCommand(
                expected_revision=current.revision, status=status, note=note
            ),
            idempotency_key=key,
        )


class InquirySourcePolicy(CurrentBusinessReplyPolicy):
    """单条原件读取仍逐次重核当前邮箱owner与本轮自动化授权。"""

    def __init__(self, records, original, contact_fingerprint):
        self.records = records
        config = records.scope
        context = SimpleNamespace(
            **config.model_dump(exclude={"actor"}),
            actor=config.actor,
            message_id=original,
        )
        super().__init__(records.source, records.path, context, contact_fingerprint)

    async def metadata(self, message_id):
        await self.records.authorize()
        meta = await super().metadata(message_id)
        async with self.source.sessions.begin() as db:
            row, box = await self.lease.acquire(db, self.scope.actor, message_id)
            history = (
                await db.scalars(
                    select(BusinessMailClassificationRow)
                    .where(
                        BusinessMailClassificationRow.tenant_id
                        == self.scope.actor.tenant_id,
                        BusinessMailClassificationRow.message_id == message_id,
                    )
                    .order_by(BusinessMailClassificationRow.revision)
                )
            ).all()
            # 在途模型可以追加历史但不能覆盖人工当前归类；任何人工纠正保留人工门槛。
            origin = (
                "human"
                if any(e.origin == "human" for e in history)
                else (
                    history[-1].origin
                    if history and history[-1].revision == row.revision
                    else None
                )
            )
            meta.update(
                classification_origin=origin,
                subject=row.subject,
                revision=row.revision,
                ingested_at=row.ingested_at.isoformat(),
                category=row.category,
                classification_status=row.classification_status,
            )
            if box.mailbox_id != self.records.scope.mailbox_id:
                raise PermissionDenied("普通询盘来源邮箱不匹配")
        return meta


class InquirySendPolicy(CurrentBusinessReplyPolicy):
    """通用询盘提案的当前授权；旧TEST-04的检查和指纹保持原样。"""

    def __init__(self, records, scope, contact_fingerprint):
        self.records = records
        super().__init__(records.source, records.path, scope, contact_fingerprint)

    async def scope_current(self, scope):
        await self.records.authorize()
        current = await self.records.view(scope.message_id)
        saved = next(
            (
                e
                for e in current.history
                if e.request_key == "inquiry-scope:" + scope.message_id
            ),
            None,
        )
        return bool(
            scope == self.scope
            and scope.policy_digest == self.records.scope.digest
            and saved
            and InquiryReplyScope.model_validate_json(saved.note) == scope
            and current.status != "completed"
            and (
                current.draft is None
                or (
                    current.draft.body_text == scope.body_text.strip()
                    and current.draft.subject == scope.subject
                )
            )
            and scope.approved_at <= datetime.now(UTC) < scope.expires_at
        )

    def source_valid(self, scope, row):
        sender = getaddresses([row.sender])
        if (
            len(sender) != 1
            or sender[0][1].casefold() != scope.recipient.casefold()
            or row.source_sha256 != scope.source_sha256
            or row.message_header != scope.original_message_id
            or row.source_kind != "full_mime"
            or row.category != "business"
            or row.classification_status != "classified"
            or row.revision != scope.source_revision
        ):
            raise ValidationError("当前询盘来源已变化")

    def limit_to_single_scope(self):
        return False  # 每封稳定键限制一封；真实发送仍必须预留身份总额度。

    def candidate_since(self, original):
        return original.ingested_at

    async def metadata(self, message_id):
        await self.records.authorize()
        return await super().metadata(message_id)
