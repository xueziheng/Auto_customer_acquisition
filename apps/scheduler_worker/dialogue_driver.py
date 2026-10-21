"""普通对话有界调度：真实待办→租约→模型→不可变计划→HIGH线程回复。"""
import asyncio
import logging
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from typing import cast

from pydantic import ValidationError as PlanValidationError
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.qualification_agent.dialogue_plan import (
    DialoguePlanDocument,
    OrdinaryDialoguePlanner,
    QuestionTopic,
)
from domains.conversations.dialogue_execution import DialogueExecutionClaim
from domains.conversations.dialogue_execution import DialogueExecutionState as State
from domains.conversations.schemas import ReplyWorkAction, ReplyWorkActionRequest
from domains.conversations.service import ConversationService
from domains.demand.schemas import ValidatedNeedView
from domains.demand.service import DemandService
from infra.db.dialogue_execution import SqlDialogueExecutionStore
from infra.db.dialogue_execution_tables import (
    DialogueBindingRow,
    DialogueExecutionRow,
    DialogueMessageRow,
)
from infra.db.dialogue_source import SqlDialogueSources
from infra.db.tables import (
    ConversationReplyWorkRow,
    MessageRow,
    RawArtifactRow,
    WorkflowRunRow,
)
from infra.standalone.settings import StandaloneModelSettings
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    IdempotencyKey,
    MessageId,
    OutboundMessageId,
    RunId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError
from shared.schemas.quote_facts import canonical_fact_hash
from tool_gateway.errors import ToolCallStatus
from tool_gateway.handlers.dialogue_send import MANIFEST
from tool_gateway.pipeline import ToolCallContext, ToolGateway
from workflows.reply_qualification.dialogue_authority import (
    CurrentOrdinaryDialogueAccess,
)
from workflows.reply_qualification.ports import MessageContentReader

from .dialogue_admission import ColdReplyDialogueAdmission
from .dialogue_business_inbound import DialogueBusinessInboundBridge
from .dialogue_models import OrdinaryDialogueModels
from .dialogue_plan_store import GatewayDialoguePlanStore

_logger = logging.getLogger(__name__)


def semantic_need_hash(need: ValidatedNeedView) -> str:
    """无进展计数只认业务值；来源/原文/确认记录仍在Need及原件闸门独立核验。"""
    snapshot = asdict(need)
    snapshot.pop('created_at')
    snapshot.pop('account_name')
    snapshot['fields'] = sorted(
        ({'name': field.name, 'value': ' '.join(field.value.split())} for field in need.fields),
        key=lambda field: (field['name'], field['value']))
    snapshot['missing_for_sourcing'] = sorted(need.missing_for_sourcing)
    return canonical_fact_hash(snapshot)


class OrdinaryDialogueDriver:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant: TenantId,
                 store: SqlDialogueExecutionStore, access: CurrentOrdinaryDialogueAccess,
                 demand: DemandService, conversations: ConversationService, content: MessageContentReader,
                 models: OrdinaryDialogueModels, plans: GatewayDialoguePlanStore, send: ToolGateway,
                 *, configuration_version: str, settings: StandaloneModelSettings, batch_limit: int = 20, admission: ColdReplyDialogueAdmission | None = None, business_inbound: DialogueBusinessInboundBridge | None = None) -> None:
        if not 1 <= batch_limit <= 100:
            raise ValidationError('普通对话批量无效')
        self.settings = settings
        self.admission = admission
        self.business_inbound = business_inbound
        self.sessions, self.tenant, self.store, self.access = sessions, tenant, store, access
        self.demand, self.conversations, self.content = demand, conversations, content
        self.models, self.plans, self.send = models, plans, send
        self.version, self.limit = configuration_version, batch_limit
        self.sources = SqlDialogueSources(sessions, tenant)

    async def progress(self, message: MessageId) -> tuple[str, tuple[str, ...]]:
        source = await self.sources.for_message(self.tenant, message)
        if source is None:
            raise PermissionDenied('普通对话来源缺失')
        await self.access.require(self.tenant, message, source.owner_employee_id)
        hypothesis = await self.demand.get_hypothesis(self.tenant, source.hypothesis_id)
        if hypothesis.account_id != source.account_id:
            raise PermissionDenied('普通对话需求归属改变')
        if hypothesis.validated_need_id is None:
            return canonical_fact_hash({'hypothesis_id':str(source.hypothesis_id), 'need_id':None}), ('application','quantity')
        need = await self.demand.get_need(self.tenant, ValidatedNeedId(hypothesis.validated_need_id))
        if need.account_id != source.account_id:
            raise PermissionDenied('普通对话需求归属改变')
        allowed = ('application','quantity','specifications','destination','product_category')
        topics = tuple(dict.fromkeys('specifications' if name in {'material','dimensions','specification'} else name
            for name in need.missing_for_sourcing if name in {*allowed,'material','dimensions','specification'}))[:2]
        return semantic_need_hash(need), topics

    async def _review(self, claim: DialogueExecutionClaim, reason: str, *, preserve_unknown: bool = False) -> None:
        source = await self.sources.for_message(self.tenant, claim.source_message_id)
        if source is None:
            raise PermissionDenied('普通对话来源缺失')
        async with self.sessions() as db:
            message = await db.get(MessageRow, (str(self.tenant), str(claim.source_message_id)))
            if message is None:
                raise PermissionDenied('普通对话原件缺失')
            outbound = message.outbound_message_id
        await self.conversations.enqueue_reply_work_action(self.tenant, ReplyWorkActionRequest(
            message_id=claim.source_message_id, outbound_message_id=OutboundMessageId(outbound) if outbound else None, enrollment_id=source.enrollment_id,
            account_id=source.account_id, contact_point_id=source.contact_point_id, dialogue_binding_id=source.binding_id,
            action=ReplyWorkAction.REVIEW_REPLY, review_reason=reason,
            idempotency_key=IdempotencyKey(f'reply:review_reply:{claim.source_message_id}')))
        if not preserve_unknown:
            await self.store.advance(claim, State.REVIEW)

    async def scan_once(self) -> int:
        """空队列只查元数据；分类流程未完成或人工已领取的待办不自动发送。"""
        if self.business_inbound is not None:
            await self.business_inbound.scan_once(self.limit)
        if self.admission is not None:
            await self.admission.scan_once(self.limit)
        if not self.settings.automatic_send_enabled:
            return 0
        async with self.sessions() as db:
            rows = (await db.execute(select(ConversationReplyWorkRow.message_id, DialogueMessageRow.binding_id)
                .join(DialogueMessageRow, (DialogueMessageRow.tenant_id==ConversationReplyWorkRow.tenant_id)
                    & (DialogueMessageRow.message_id==ConversationReplyWorkRow.message_id))
                .join(DialogueBindingRow, (DialogueBindingRow.tenant_id==DialogueMessageRow.tenant_id)
                    & (DialogueBindingRow.binding_id==DialogueMessageRow.binding_id))
                .where(ConversationReplyWorkRow.tenant_id==self.tenant, DialogueMessageRow.tenant_id==self.tenant,
                    DialogueBindingRow.tenant_id==self.tenant, DialogueBindingRow.state=='active',
                    DialogueBindingRow.latest_message_id==ConversationReplyWorkRow.message_id,
                    ConversationReplyWorkRow.status=='pending', ConversationReplyWorkRow.action.in_(('start_qualification','create_follow_up')),
                    exists(select(WorkflowRunRow.run_id).where(WorkflowRunRow.tenant_id==self.tenant,
                        WorkflowRunRow.subject_ref==ConversationReplyWorkRow.message_id,
                        WorkflowRunRow.workflow_type=='reply_qualification', WorkflowRunRow.status=='completed')),
                    ~exists(select(DialogueExecutionRow.source_message_id).where(DialogueExecutionRow.tenant_id==self.tenant,
                        DialogueExecutionRow.source_message_id==ConversationReplyWorkRow.message_id)))
                .order_by(ConversationReplyWorkRow.created_at).limit(self.limit))).all()
        for message, binding in rows:
            await self.store.enqueue(MessageId(message), binding)
        handled = 0
        for _ in range(self.limit):
            claim = await self.store.claim(lease_duration=timedelta(minutes=10))
            if claim is None:
                break
            try:
                await self._process(claim)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 -- 外调未知不会重试；当前身份失败只落固定原因。
                latest = await self.store.read(claim.source_message_id)
                if latest and latest.lease_token==claim.lease_token:
                    if await self._cancel_superseded(latest):
                        pass
                    elif latest.state is State.MODEL_STARTED:
                        if isinstance(error, (PlanValidationError, ValidationError)):
                            await self._review(latest, 'invalid_plan')
                        elif isinstance(error, (PermissionDenied, ModelGenerationError)) and getattr(error, 'code', None) in {'permission', 'configuration', 'quota', 'invalid_request'}:
                            await self._review(latest, 'budget_exhausted' if getattr(error, 'code', None) == 'quota' else 'scope_changed')
                        else:
                            unknown = await self.store.advance(latest, State.UNKNOWN)
                            await self._review(unknown, 'model_result_unknown', preserve_unknown=True)
                    elif latest.state is State.SENDING:
                        await self.store.advance(latest, State.UNKNOWN)
                    elif latest.state in {State.CLAIMED, State.PLANNED}:
                        await self._review(latest, 'scope_changed')
            handled += 1
        async with self.sessions() as db:
            unresolved = (await db.scalars(select(DialogueExecutionRow.source_message_id).where(
                DialogueExecutionRow.tenant_id == self.tenant, DialogueExecutionRow.state == 'unknown',
                DialogueExecutionRow.attempt_id.is_(None),
                ~exists(select(ConversationReplyWorkRow.action_id).where(
                    ConversationReplyWorkRow.tenant_id == self.tenant,
                    ConversationReplyWorkRow.message_id == DialogueExecutionRow.source_message_id,
                    ConversationReplyWorkRow.action == 'review_reply'))).limit(self.limit))).all()
        for message in unresolved:
            unresolved_claim = await self.store.read(MessageId(message))
            if unresolved_claim is not None:
                await self._review(unresolved_claim, 'model_result_unknown', preserve_unknown=True)
        for claim in await self.store.reconciliation_candidates(limit=self.limit):
            try:
                await self._send(claim)
            except Exception:  # noqa: BLE001 -- 未知只核对原attempt，不创建新执行。
                _logger.info("普通回复投递结果仍待核对")
        return handled

    async def _cancel_superseded(self, claim: DialogueExecutionClaim) -> bool:
        """新入站只废弃尚未派发的旧执行；保留预算，让新消息继续原canonical处理。"""
        if claim.state not in {State.CLAIMED, State.MODEL_STARTED, State.PLANNED}:
            return False
        source = await self.sources.for_message(self.tenant, claim.source_message_id)
        if source is None:
            return False
        try:
            owner = await self.access.owners.require(self.tenant, claim.source_message_id, source.owner_employee_id)
        except PermissionDenied:
            return False
        if owner.pending_inbound or owner.latest_message_id != claim.source_message_id:
            await self.store.advance(claim, State.CANCELLED)
            return True
        return False

    async def _process(self, claim: DialogueExecutionClaim) -> None:
        source = await self.sources.for_message(self.tenant, claim.source_message_id)
        if source is None:
            raise PermissionDenied('普通对话来源缺失')
        current = await self.access.current(self.tenant, claim.source_message_id, source.owner_employee_id)
        if current.owner.rounds_sent >= current.policy.max_rounds or (claim.state is State.CLAIMED and current.owner.model_calls >= current.policy.max_model_calls):
            await self._review(claim, 'budget_exhausted')
            return
        if current.owner.no_progress_rounds >= current.policy.max_no_progress_rounds:
            await self._review(claim, 'no_progress')
            return
        if claim.state is State.CLAIMED:
            content = await self.content.load(self.tenant, claim.source_message_id)
            if content is None:
                raise PermissionDenied('普通对话原文缺失')
            progress, topics = await self.progress(claim.source_message_id)
            async with self.sessions() as db:
                message = await db.get(MessageRow, (str(self.tenant), str(claim.source_message_id)))
                raw = await db.get(RawArtifactRow, (str(self.tenant), message.raw_artifact_ref)) if message else None
                if raw is None:
                    raise PermissionDenied('普通对话原件缺失')
                source_hash = raw.content_hash
            claim = await self.store.advance(claim, State.MODEL_STARTED)
            identity = InvocationIdentity(tenant_id=self.tenant, employee_id=source.owner_employee_id,
                user_id=current.owner.user_id, run_id=RunId(claim.run_id), capability='ordinary_dialogue',
                configuration_version=self.version, sequence=claim.model_sequence)
            model, generator = await self.models.select(identity, claim.source_message_id)
            plan = await OrdinaryDialoguePlanner(generator).plan(identity, model=model,
                customer_subject=content.subject or '', customer_text=content.body, products=current.products,
                allowed_questions=topics, max_output_tokens=current.policy.max_output_tokens)
            await self.access.require(self.tenant, claim.source_message_id, source.owner_employee_id)
            if plan.kind=='review':
                await self._review(claim, 'insufficient_evidence' if plan.review_reason=='unsupported_product' else plan.review_reason or 'insufficient_evidence')
                return
            document = DialoguePlanDocument(plan=plan, source_sha256=source_hash,
                need_progress_sha256=progress, allowed_questions=cast(tuple[QuestionTopic,...], topics))
            async def recheck() -> None:
                await self.access.require(self.tenant, claim.source_message_id, source.owner_employee_id)
                row = await self.store.read(claim.source_message_id)
                if row != claim:
                    raise PermissionDenied('普通计划租约改变')
            artifact = await self.plans.put(claim, current.owner.user_id, document.model_dump_json().encode(), recheck)
            claim = await self.store.advance(claim, State.PLANNED, plan_artifact_id=str(artifact.artifact_id),
                plan_sha256=artifact.content_hash, need_progress_sha256=progress)
        if claim.attempt_id is None:
            digest = sha256(f'{self.tenant}:{claim.source_message_id}'.encode()).hexdigest()
            claim = await self.store.prepare_send(claim, attempt_id=new_id('mat'),
                deterministic_message_id=f'<{digest}@messages.tradeos.invalid>', idempotency_key=f'dialogue:{digest}')
        await self._send(claim)

    async def _send(self, claim: DialogueExecutionClaim) -> None:
        source = await self.sources.for_message(self.tenant, claim.source_message_id)
        if source is None or claim.attempt_id is None or claim.idempotency_key is None:
            raise PermissionDenied('普通回复发送身份缺失')
        scope = await self.access.current(self.tenant, claim.source_message_id, source.owner_employee_id)
        result = await self.send.invoke(ToolCallContext(tenant_id=self.tenant, user_id=scope.owner.user_id,
            run_id=RunId(claim.run_id), tool_id=MANIFEST.tool_id,
            params={'message_id':str(claim.source_message_id), 'attempt_id':claim.attempt_id, 'lease_token':claim.lease_token},
            idempotency_key=IdempotencyKey(claim.idempotency_key), campaign_ref=source.campaign_id))
        if result.status is not ToolCallStatus.SUCCEEDED:
            latest = await self.store.read(claim.source_message_id)
            if latest and latest.state is State.SENDING:
                await self.store.advance(latest, State.UNKNOWN)
            elif latest and latest.state is State.PLANNED and not await self._cancel_superseded(latest):
                await self._review(latest, 'scope_changed')
