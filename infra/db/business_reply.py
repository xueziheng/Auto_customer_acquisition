"""单封询盘回复的当前授权与账本读取；不创建客户、Campaign或需求。"""

from datetime import UTC, datetime
from email.utils import getaddresses
from pathlib import Path

from sqlalchemy import func, or_, select, text

from domains.conversations.business_reply import BusinessReplyScope, validate_source
from infra.db.business_mail_followup import CurrentBusinessMailLease
from infra.db.business_mail_tables import BusinessMailRow
from infra.db.tables import (
    ContactPointRow,
    OutreachSuppressionRow,
    ProspectContactRow,
    SendingIdentityRow,
    ToolCallRow,
)
from infra.pilot.config import private_read
from shared.errors import PermissionDenied, ValidationError


class CurrentBusinessReplyPolicy:
    def __init__(
        self, source, scope_path: Path, scope: BusinessReplyScope, contact_fingerprint
    ):
        self.source, self.scope_path, self.scope = source, scope_path, scope
        self.lease = CurrentBusinessMailLease(source)
        self.contact_fingerprint = contact_fingerprint

    async def scope_current(self, scope):
        return (
            scope == self.scope
            and BusinessReplyScope.model_validate_json(private_read(self.scope_path))
            == scope
            and scope.approved_at <= datetime.now(UTC) < scope.expires_at
        )

    def source_valid(self, scope, row):
        validate_source(
            scope,
            sender=row.sender,
            subject=row.subject,
            body=row.body_text,
            header=row.message_header,
            source_sha256=row.source_sha256,
            source_kind=row.source_kind,
            category=row.category,
            status=row.classification_status,
        )

    def limit_to_single_scope(self):
        return True

    async def check(self, stage, scope):
        if not await self.scope_current(scope):
            return False
        async with self.source.sessions.begin() as db:
            row, box = await self.lease.acquire(db, scope.actor, scope.message_id)
            await self.lease.reject_ordinary_dialogue(db, scope.actor, scope.message_id)
            if (
                box.address != scope.sender
                or box.binding_operation_id != scope.binding_operation_id
            ):
                return False
            self.source_valid(scope, row)
            identity = await db.scalar(
                select(SendingIdentityRow).where(
                    SendingIdentityRow.tenant_id == scope.actor.tenant_id,
                    SendingIdentityRow.identity_id == scope.identity_id,
                    SendingIdentityRow.address == scope.sender,
                )
            )
            if (
                identity is None
                or identity.state not in {"warming", "active"}
                or identity.version != scope.identity_version
            ):
                return False
            count = await db.scalar(
                select(func.count())
                .select_from(ContactPointRow)
                .join(
                    ProspectContactRow,
                    (ProspectContactRow.tenant_id == ContactPointRow.tenant_id)
                    & (ProspectContactRow.contact_id == ContactPointRow.contact_id),
                )
                .join(
                    OutreachSuppressionRow,
                    (OutreachSuppressionRow.tenant_id == ContactPointRow.tenant_id)
                    & or_(
                        OutreachSuppressionRow.contact_point_id
                        == ContactPointRow.contact_point_id,
                        OutreachSuppressionRow.account_id
                        == ProspectContactRow.account_id,
                    ),
                )
                .where(
                    ContactPointRow.tenant_id == scope.actor.tenant_id,
                    ProspectContactRow.tenant_id == scope.actor.tenant_id,
                    OutreachSuppressionRow.tenant_id == scope.actor.tenant_id,
                    ContactPointRow.kind == "email",
                    func.lower(ContactPointRow.value) == scope.recipient.casefold(),
                )
            )
            if count != 0:
                return False
            # 与原Prospecting完全相同的域分隔HMAC，删除邮箱也不得绕过抑制。
            erased = await db.scalar(
                text(
                    "SELECT count(*) FROM prospecting_erasure_suppressions WHERE tenant_id=:tenant AND value_hash=:digest"
                ),
                {
                    "tenant": str(scope.actor.tenant_id),
                    "digest": self.contact_fingerprint(scope.recipient.casefold()),
                },
            )
            if erased != 0:
                return False
            # 此独立范围最多一封；同原信稳定键由原Gateway跨进程持久去重。
            if stage == "rate_limit" and self.limit_to_single_scope():
                others = await db.scalar(
                    select(func.count())
                    .select_from(ToolCallRow)
                    .where(
                        ToolCallRow.tenant_id == scope.actor.tenant_id,
                        ToolCallRow.tool_id == "email.business.reply",
                        ToolCallRow.user_id == scope.actor.employee_id,
                        ToolCallRow.idempotency_key.is_not(None),
                        ToolCallRow.idempotency_key != scope.key,
                        ToolCallRow.created_at >= scope.approved_at,
                        ToolCallRow.status.in_(
                            ["claimed", "executing", "succeeded", "failed_transient"]
                        ),
                    )
                )
                if others:
                    return False
            await self.lease.check(scope.actor, box)
        return True

    async def authorize(self):
        if not await self.check("permission", self.scope):
            raise PermissionDenied("当前询盘回复授权已失效")

    async def metadata(self, message_id):
        async with self.source.sessions.begin() as db:
            row, box = await self.lease.acquire(db, self.scope.actor, message_id)
            await self.lease.reject_ordinary_dialogue(db, self.scope.actor, message_id)
            if (
                box.address != self.scope.sender
                or box.binding_operation_id != self.scope.binding_operation_id
            ):
                raise PermissionDenied("当前邮箱范围已改变")
            if row.source_kind != "full_mime":
                raise ValidationError("邮件原件不完整")
            return {
                "message_id": row.message_id,
                "artifact_id": row.artifact_id,
                "source_sha256": row.source_sha256,
                "body": row.body_text,
                "header": row.message_header,
                "sender": row.sender,
                "received_at": row.received_at.isoformat(),
            }

    def candidate_since(self, original):
        return self.scope.approved_at

    async def candidates(self):
        await self.source.detail(self.scope.actor, self.scope.message_id)
        async with self.source.sessions.begin() as db:
            _original, box = await self.lease.acquire(
                db, self.scope.actor, self.scope.message_id
            )
            rows = (
                await db.scalars(
                    select(BusinessMailRow)
                    .where(
                        BusinessMailRow.tenant_id == self.scope.actor.tenant_id,
                        BusinessMailRow.mailbox_id == box.mailbox_id,
                        BusinessMailRow.ingested_at >= self.candidate_since(_original),
                        BusinessMailRow.message_id != self.scope.message_id,
                        BusinessMailRow.source_kind == "full_mime",
                    )
                    .order_by(BusinessMailRow.ingested_at.desc())
                    .limit(50)
                )
            ).all()
            return [
                r.message_id
                for r in rows
                if getaddresses([r.sender])
                and getaddresses([r.sender])[0][1].casefold()
                == self.scope.recipient.casefold()
            ]

    async def send_record(self):
        async with self.source.sessions() as db:
            row = await db.scalar(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == self.scope.actor.tenant_id,
                    ToolCallRow.tool_id == "email.business.reply",
                    ToolCallRow.idempotency_key == self.scope.key,
                )
            )
            return (
                None
                if row is None
                else {
                    "tool_call_id": row.tool_call_id,
                    "status": row.status,
                    "provider_ref": row.provider_ref,
                    "error_category": row.error_category,
                }
            )
