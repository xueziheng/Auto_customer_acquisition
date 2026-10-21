"""普通线程回复真实发送端口：当前授权、不可变计划、共享额度与QQ协议。"""
from collections.abc import Callable
from hashlib import sha256
from typing import Protocol

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.qualification_agent.dialogue_plan import (
    DialoguePlanDocument,
    render_dialogue,
)
from artifact_store.store import BoundedGeneratedArtifactStore, GeneratedArtifactKind
from connectors.qq_mail.reply_client import QQBusinessReplyConnector
from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.dialogue_execution import DialogueExecutionState as State
from domains.sending_identity.permissions import Actor, ScopeLevel, SendingIdentityScope
from domains.sending_identity.service import SendingIdentityService
from infra.business_mail_binding import CurrentBusinessMailboxBinding
from infra.db.dialogue_execution import SqlDialogueExecutionStore
from infra.db.dialogue_source import SqlDialogueSources
from infra.db.tables import (
    ContactPointRow,
    MessageRow,
    RawArtifactRow,
    SendingIdentityRow,
)
from infra.standalone.settings import StandaloneModelSettings
from shared.errors import PermissionDenied
from shared.schemas.business_reply import SMTPReplyRequest, validate_reply_message_id
from shared.schemas.identifiers import ArtifactId, IdempotencyKey, MessageId, TenantId
from tool_gateway.handlers.dialogue_send import DialogueDelivery, ThreadReplyConnector
from workflows.reply_qualification.dialogue_authority import (
    CurrentOrdinaryDialogueAccess,
)


class NeedProgressReader(Protocol):
    async def progress(self, message: MessageId) -> tuple[str, tuple[str, ...]]: ...


class CurrentDialogueDelivery:
    """凭证仅在handler开始执行/核对后解析；旧cold connector从不作为fallback。"""
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant: TenantId,
                 access: CurrentOrdinaryDialogueAccess, store: SqlDialogueExecutionStore,
                 artifacts: BoundedGeneratedArtifactStore, sending: SendingIdentityService,
                 mailbox: CurrentBusinessMailboxBinding, progress: NeedProgressReader,
                 connector_factory: Callable[[str, SecretStr], ThreadReplyConnector] = QQBusinessReplyConnector,
                 *, settings: StandaloneModelSettings) -> None:
        self.sessions, self.tenant, self.access, self.store = sessions, tenant, access, store
        self.artifacts, self.sending, self.mailbox, self.progress_reader = artifacts, sending, mailbox, progress
        self.connector_factory = connector_factory
        self.settings = settings
        self.sources = SqlDialogueSources(sessions, tenant)

    def _require_enabled(self) -> None:
        if not self.settings.automatic_send_enabled:
            raise PermissionDenied("全局自动发送已关闭")

    async def current(self, tenant: TenantId, message: MessageId, attempt: str, lease_token: str) -> DialogueDelivery:
        self._require_enabled()
        if tenant != self.tenant:
            raise PermissionDenied('普通回复租户拒绝')
        source = await self.sources.for_message(tenant, message)
        row = await self.store.read(message)
        if (source is None or row is None or row.attempt_id != attempt or row.lease_token != lease_token
            or row.state not in {State.PLANNED, State.SENDING, State.UNKNOWN, State.SENT}
            or row.plan_artifact_id is None or row.plan_sha256 is None or row.idempotency_key is None
            or row.deterministic_message_id is None):
            raise PermissionDenied('普通回复执行绑定拒绝')
        scope = await self.access.current(tenant, message, source.owner_employee_id)
        if row.state is State.PLANNED and (scope.owner.rounds_sent >= scope.policy.max_rounds
            or scope.owner.no_progress_rounds >= scope.policy.max_no_progress_rounds):
            raise PermissionDenied('普通回复轮数已用完')
        meta, content = await self.artifacts.get_bounded(tenant, ArtifactId(row.plan_artifact_id), maximum_bytes=32768)
        if (meta.kind is not GeneratedArtifactKind.DIALOGUE_PLAN or meta.content_hash != row.plan_sha256
            or meta.workflow_run_id != row.run_id or meta.subject_ref != message or meta.sequence_number != row.model_sequence + 1):
            raise PermissionDenied('普通回复计划原件绑定拒绝')
        document = DialoguePlanDocument.model_validate_json(content)
        progress, questions = await self.progress_reader.progress(message)
        if progress != document.need_progress_sha256 or tuple(questions[:2]) != document.allowed_questions:
            raise PermissionDenied('普通回复需求事实已变化')
        async with self.sessions() as db:
            original = await db.get(MessageRow, (str(tenant), str(message)))
            identity = await db.get(SendingIdentityRow, (str(tenant), str(source.sending_identity_id)))
            contact = await db.get(ContactPointRow, (str(tenant), str(source.contact_point_id)))
            raw = await db.get(RawArtifactRow, (str(tenant), original.raw_artifact_ref)) if original else None
            if (original is None or identity is None or contact is None or raw is None
                or raw.content_hash != document.source_sha256 or raw.kind != 'email_raw'):
                raise PermissionDenied('普通回复客户原件已变化')
            validate_reply_message_id(original.external_message_id)
            request = SMTPReplyRequest(identity.address, contact.value, 'Re: Your enquiry',
                render_dialogue(document.plan, scope.products, allowed_questions=questions),
                row.deterministic_message_id[1:-1], sha256(row.idempotency_key.encode()).hexdigest(),
                original.external_message_id, (original.external_message_id,))
        await self.access.require(tenant, message, source.owner_employee_id)
        return DialogueDelivery(tenant, message, attempt, source.owner_employee_id, scope.owner.user_id,
            row.idempotency_key, row.plan_sha256, lease_token, request)

    async def reserve(self, delivery: DialogueDelivery) -> None:
        self._require_enabled()
        claim = await self.store.read(delivery.message_id)
        source = await self.sources.for_message(delivery.tenant_id, delivery.message_id)
        if claim is None or source is None or claim.lease_token != delivery.lease_token:
            raise PermissionDenied('普通回复额度绑定拒绝')
        await self.store.reserve_campaign_slot(claim)
        await self.sending.reserve_send_slot(delivery.tenant_id, source.sending_identity_id,
            IdempotencyKey(delivery.idempotency_key), False,
            actor=Actor('system:ordinary-dialogue', SendingIdentityScope(level=ScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({source.sending_identity_id})), 'system'))

    async def started(self, delivery: DialogueDelivery) -> None:
        self._require_enabled()
        claim = await self.store.read(delivery.message_id)
        if claim is None or claim.state is not State.PLANNED or claim.lease_token != delivery.lease_token:
            raise PermissionDenied('普通回复不能再次派发')
        await self.store.advance(claim, State.SENDING)

    async def connector(self, delivery: DialogueDelivery) -> ThreadReplyConnector:
        self._require_enabled()
        source = await self.sources.for_message(delivery.tenant_id, delivery.message_id)
        claim = await self.store.read(delivery.message_id)
        if source is None or claim is None or claim.state not in {State.SENDING, State.UNKNOWN}:
            raise PermissionDenied('普通回复仅执行或核对时建立连接')
        await self.access.require(delivery.tenant_id, delivery.message_id, delivery.employee_id)
        actor = BusinessMailActor(tenant_id=delivery.tenant_id, employee_id=delivery.employee_id, user_id=delivery.user_id)
        reference = 'BUSINESS_QQ_MAIL'
        password = self.mailbox.resolver(actor, delivery.request.from_address, source.mailbox_operation_id).resolve(reference)
        return self.connector_factory(delivery.request.from_address, SecretStr(password))

    async def sent(self, delivery: DialogueDelivery, provider_ref: str) -> None:
        await self.store.reconcile_sent(delivery.message_id, attempt_id=delivery.attempt_id,
            deterministic_message_id=f'<{delivery.request.deterministic_message_id}>', provider_ref=provider_ref)
