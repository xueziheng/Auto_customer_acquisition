"""普通回复反馈仅沿精确SENT来源，不借冷Attempt/Enrollment伪造关联。"""
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    SuppressionReason,
    SuppressionRequest,
    SuppressionTarget,
)
from domains.outreach.service import OutreachService
from domains.sending_identity.service import Actor as IdentityActor
from domains.sending_identity.service import (
    DeliveryEventRecord,
    DeliveryEventType,
    SendingIdentityScope,
    SendingIdentityService,
)
from domains.sending_identity.service import ScopeLevel as IdentityScope
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.dialogue_source import SqlDialogueSources
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId
from workflows.reply_qualification.ports import ReplyActionContext


class DialogueReplyFeedback:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], outreach: OutreachService,
                 identities: SendingIdentityService) -> None:
        self.sessions, self.outreach, self.identities = sessions, outreach, identities

    async def apply(self, tenant: TenantId, context: ReplyActionContext,
                    kind: Literal['bounce', 'complaint']) -> None:
        """已分类客户反馈仍重读原分类；失败可按相同消息恢复，信誉/抑制各有持久幂等。"""
        reader = SqlDialogueSources(self.sessions, tenant)
        source = await reader.for_message(tenant, context.message_id)
        sent = await reader.for_sent_header(tenant, str(context.outbound_message_id)) if context.outbound_message_id else None
        if (source is None or sent != source or source.binding_id != context.dialogue_binding_id
            or source.account_id != context.account_id or source.contact_point_id != context.contact_point_id):
            raise ValidationError('普通回复反馈缺少精确SENT来源')
        async with SqlAlchemyConversationsUnitOfWork(self.sessions, tenant, now=lambda:datetime.now(UTC)) as uow:
            classification = await uow.classifications.get(tenant, context.message_id)
            if classification is None or classification.category.value != kind:
                raise ValidationError('普通回复反馈缺少持久分类')
        event = sha256(f'dialogue-feedback:{tenant}:{context.message_id}:{kind}'.encode()).hexdigest()
        identity = source.sending_identity_id
        await self.identities.record_delivery_event(tenant, identity, DeliveryEventRecord(
            tenant_id=tenant, identity_id=identity,
            event_type=DeliveryEventType.HARD_BOUNCED if kind == 'bounce' else DeliveryEventType.COMPLAINT,
            occurred_at=classification.classified_at, dedup_key=IdempotencyKey(event), source_ref=f'dlgfb_{event[:56]}'),
            actor=IdentityActor('system:dialogue-feedback', SendingIdentityScope(level=IdentityScope.SYSTEM,
                allowed_identity_ids=frozenset({identity})), 'system'))
        target = SuppressionTarget(contact_point_id=source.contact_point_id)
        await self.outreach.add_suppression(tenant, SuppressionRequest(target=target,
            reason=SuppressionReason.HARD_BOUNCE if kind == 'bounce' else SuppressionReason.COMPLAINT,
            source_ref=f'dlgfb_{event[:56]}', occurred_at=classification.classified_at,
            idempotency_key=IdempotencyKey(f'dialogue-feedback:{event}')),
            actor=Actor('system:dialogue-feedback', OutreachScope(level=ScopeLevel.SYSTEM,
                allowed_suppression_targets=frozenset({target.canonical_id})), 'system'))
        async with SqlAlchemyConversationsUnitOfWork(self.sessions, tenant, now=lambda:datetime.now(UTC)) as uow:
            await uow.pause_dialogue(tenant, context.message_id)
