"""对话执行的短事务领取与fencing；不承担授权或外部调用。"""
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.dialogue_execution import (
    DialogueExecutionClaim,
    DialogueExecutionState,
    require_dialogue_transition,
)
from infra.db.dialogue_execution_tables import DialogueBindingRow, DialogueExecutionRow
from infra.db.tables import MessageRow
from shared.errors import ValidationError
from shared.schemas.identifiers import MessageId, TenantId, new_id


class SqlDialogueExecutionStore:
    """每源消息只有一个稳定调用身份，expired外调只转未知而不重派。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant_id: TenantId,
                 *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.sessions, self.tenant_id, self.now = sessions, tenant_id, now

    def _claim(self, row: DialogueExecutionRow) -> DialogueExecutionClaim:
        if row.lease_token is None or row.lease_until is None:
            raise ValidationError("对话执行租约缺失")
        return DialogueExecutionClaim(
            tenant_id=self.tenant_id, source_message_id=MessageId(row.source_message_id),
            binding_id=row.binding_id, run_id=row.run_id, model_sequence=row.model_sequence,
            state=DialogueExecutionState(row.state), version=row.version,
            lease_token=row.lease_token, lease_until=row.lease_until,
            plan_artifact_id=row.plan_artifact_id, plan_sha256=row.plan_sha256,
            attempt_id=row.attempt_id, deterministic_message_id=row.deterministic_message_id,
            idempotency_key=row.idempotency_key, provider_ref=row.provider_ref,
            need_progress_sha256=row.need_progress_sha256,
        )

    async def enqueue(self, message_id: MessageId, binding_id: str) -> None:
        """同一消息跨action唯一；只有真实入站消息可以成为自动执行源。"""
        if not binding_id or len(binding_id) > 40:
            raise ValidationError("对话绑定标识无效")
        async with self.sessions.begin() as db:
            message = await db.get(MessageRow, (str(self.tenant_id), str(message_id)))
            if message is None or message.direction != "inbound":
                raise ValidationError("对话执行源不是本企业入站消息")
            await db.execute(insert(DialogueExecutionRow).values(
                tenant_id=str(self.tenant_id), source_message_id=str(message_id),
                binding_id=binding_id, run_id=new_id("run"), model_sequence=0,
                state="queued", version=0, created_at=self.now(),
            ).on_conflict_do_nothing(index_elements=["tenant_id", "source_message_id"]))
            row = await db.get(DialogueExecutionRow, (str(self.tenant_id), str(message_id)))
            if row is None or row.binding_id != binding_id:
                raise ValidationError("对话源消息绑定冲突")

    async def claim(self, *, lease_duration: timedelta) -> DialogueExecutionClaim | None:
        """有界领取一项；回收尚未外调任务，外调中崩溃一律只核对。"""
        if not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=10):
            raise ValidationError("对话执行租期无效")
        now = self.now()
        async with self.sessions.begin() as db:
            await db.execute(update(DialogueExecutionRow).where(
                DialogueExecutionRow.tenant_id == str(self.tenant_id),
                DialogueExecutionRow.state.in_(("model_started", "sending")),
                DialogueExecutionRow.lease_until <= now,
            ).values(state="unknown", version=DialogueExecutionRow.version + 1))
            row = await db.scalar(select(DialogueExecutionRow).where(
                DialogueExecutionRow.tenant_id == str(self.tenant_id),
                or_(DialogueExecutionRow.state == "queued",
                    (DialogueExecutionRow.state.in_(("claimed", "planned"))) & (DialogueExecutionRow.lease_until <= now)),
            ).order_by(DialogueExecutionRow.created_at, DialogueExecutionRow.source_message_id)
              .limit(1).with_for_update(skip_locked=True))
            if row is None:
                return None
            if row.state != "planned":
                row.state = "claimed"
            row.lease_token, row.lease_until = new_id("dlg"), now + lease_duration
            row.version += 1
            return self._claim(row)

    async def advance(self, claim: DialogueExecutionClaim, state: DialogueExecutionState,
                      *, plan_artifact_id: str | None = None, plan_sha256: str | None = None,
                      attempt_id: str | None = None, deterministic_message_id: str | None = None,
                      idempotency_key: str | None = None, provider_ref: str | None = None,
                      need_progress_sha256: str | None = None) -> DialogueExecutionClaim:
        """每次转换比较租户/token/version/expiry，外调身份与不可变计划不可改写。"""
        if claim.tenant_id != self.tenant_id:
            raise ValidationError("对话执行租户不匹配")
        require_dialogue_transition(claim.state, state)
        async with self.sessions.begin() as db:
            row = await db.scalar(select(DialogueExecutionRow).where(
                DialogueExecutionRow.tenant_id == str(self.tenant_id),
                DialogueExecutionRow.source_message_id == str(claim.source_message_id),
                DialogueExecutionRow.version == claim.version,
                DialogueExecutionRow.lease_token == claim.lease_token,
                DialogueExecutionRow.lease_until > self.now(),
                DialogueExecutionRow.state == claim.state.value,
            ).with_for_update())
            if row is None:
                raise ValidationError("对话执行租约已失效")
            changes = {"plan_artifact_id": plan_artifact_id, "plan_sha256": plan_sha256,
                       "attempt_id": attempt_id, "deterministic_message_id": deterministic_message_id,
                       "idempotency_key": idempotency_key, "provider_ref": provider_ref,
                       "need_progress_sha256": need_progress_sha256}
            for name, value in changes.items():
                if value is not None:
                    if getattr(row, name) not in (None, value):
                        raise ValidationError("对话不可变执行事实冲突")
                    setattr(row, name, value)
            if state is DialogueExecutionState.PLANNED and not (row.plan_artifact_id and row.plan_sha256):
                raise ValidationError("对话计划缺少不可变原件")
            if state in {DialogueExecutionState.SENDING, DialogueExecutionState.SENT} and not (
                row.plan_artifact_id and row.attempt_id and row.deterministic_message_id and row.idempotency_key
            ):
                raise ValidationError("对话发送身份未持久化")
            if state is DialogueExecutionState.SENT and not row.provider_ref:
                raise ValidationError("对话发送结果缺少供应商证据")
            if state is DialogueExecutionState.SENT:
                await self._count_sent(db, row)
            row.state, row.version = state.value, row.version + 1
            return self._claim(row)

    async def read(self, message_id: MessageId) -> DialogueExecutionClaim | None:
        """核对仅取原耐久身份，不赋予发送/模型重试许可。"""
        async with self.sessions() as db:
            row = await db.get(DialogueExecutionRow, (str(self.tenant_id), str(message_id)))
            return self._claim(row) if row is not None and row.lease_token else None

    async def reconcile_sent(self, message_id: MessageId, *, attempt_id: str,
                             deterministic_message_id: str, provider_ref: str) -> DialogueExecutionClaim:
        """精确已持久发送身份的对账完成；绝不创建新身份或恢复模型调用。"""
        if not provider_ref or len(provider_ref) > 256:
            raise ValidationError("对话发送对账证据无效")
        async with self.sessions.begin() as db:
            row = await db.scalar(select(DialogueExecutionRow).where(
                DialogueExecutionRow.tenant_id == str(self.tenant_id),
                DialogueExecutionRow.source_message_id == str(message_id),
                DialogueExecutionRow.attempt_id == attempt_id,
                DialogueExecutionRow.deterministic_message_id == deterministic_message_id,
                DialogueExecutionRow.state.in_(("sending", "unknown", "sent")),
            ).with_for_update())
            if row is None or row.provider_ref not in (None, provider_ref):
                raise ValidationError("对话发送对账关联冲突")
            if row.state != "sent":
                await self._count_sent(db, row)
                row.state, row.provider_ref = "sent", provider_ref
                row.version += 1
            return self._claim(row)


    async def _count_sent(self, db: AsyncSession, row: DialogueExecutionRow) -> None:
        binding = await db.get(DialogueBindingRow, (str(self.tenant_id), row.binding_id), with_for_update=True)
        if binding is None:
            raise ValidationError("普通对话来源丢失")
        binding.rounds_sent += 1
        binding.no_progress_rounds = (binding.no_progress_rounds + 1
            if binding.last_need_progress_sha256 == row.need_progress_sha256 else 0)
        binding.last_need_progress_sha256 = row.need_progress_sha256

    async def prepare_send(self, claim: DialogueExecutionClaim, *, attempt_id: str,
                           deterministic_message_id: str, idempotency_key: str) -> DialogueExecutionClaim:
        """仅planned租约可绑定不可变发送身份；在Gateway EXECUTING前先提交。"""
        from shared.schemas.business_reply import validate_reply_message_id
        validate_reply_message_id(deterministic_message_id)
        if claim.tenant_id != self.tenant_id or not attempt_id or not idempotency_key:
            raise ValidationError("普通回复发送身份无效")
        async with self.sessions.begin() as db:
            row = await db.scalar(select(DialogueExecutionRow).where(
                DialogueExecutionRow.tenant_id == self.tenant_id,
                DialogueExecutionRow.source_message_id == claim.source_message_id,
                DialogueExecutionRow.state == 'planned', DialogueExecutionRow.version == claim.version,
                DialogueExecutionRow.lease_token == claim.lease_token,
                DialogueExecutionRow.lease_until > self.now()).with_for_update())
            if row is None:
                raise ValidationError("对话执行租约已失效")
            for key, value in {'attempt_id':attempt_id, 'deterministic_message_id':deterministic_message_id,
                               'idempotency_key':idempotency_key}.items():
                if getattr(row, key) not in (None, value):
                    raise ValidationError("普通对话发送身份不可改写")
                setattr(row, key, value)
            row.version += 1
            return self._claim(row)

    async def reconciliation_candidates(self, *, limit: int) -> tuple[DialogueExecutionClaim, ...]:
        """仅返回已持久发送身份的未知项；未知模型不重新规划。"""
        if not 1 <= limit <= 100:
            raise ValidationError("对话核对批量无效")
        async with self.sessions() as db:
            rows = (await db.scalars(select(DialogueExecutionRow).where(
                DialogueExecutionRow.tenant_id == self.tenant_id,
                DialogueExecutionRow.state == 'unknown', DialogueExecutionRow.attempt_id.is_not(None))
                .order_by(DialogueExecutionRow.created_at, DialogueExecutionRow.source_message_id).limit(limit))).all()
            return tuple(self._claim(row) for row in rows)


    async def reserve_campaign_slot(self, claim: DialogueExecutionClaim) -> None:
        """同attempt占原Campaign总量；持久预占跨重建/未知不重复、不退款。"""
        from domains.outreach.errors import CampaignQuotaExceededError
        from domains.outreach.repository import QuotaReservationStatus
        from infra.db.repositories.outreach import QuotaRepositoryImpl
        from infra.db.tables import OutreachCampaignRow, OutreachCampaignVersionRow
        from shared.schemas.identifiers import CampaignId
        if claim.tenant_id != self.tenant_id or claim.attempt_id is None:
            raise ValidationError("普通回复额度身份无效")
        async with self.sessions.begin() as db:
            row = await db.scalar(select(DialogueExecutionRow).where(
                DialogueExecutionRow.tenant_id == self.tenant_id,
                DialogueExecutionRow.source_message_id == claim.source_message_id,
                DialogueExecutionRow.attempt_id == claim.attempt_id).with_for_update())
            if row is None or row.state not in {'planned', 'sending', 'unknown', 'sent'}:
                raise ValidationError("普通回复尚无持久发送身份")
            binding = await db.get(DialogueBindingRow, (str(self.tenant_id), row.binding_id))
            if binding is None:
                raise ValidationError("普通回复来源不存在")
            campaign = await db.get(OutreachCampaignRow, (str(self.tenant_id), binding.campaign_id), with_for_update=True)
            version = await db.get(OutreachCampaignVersionRow,
                (str(self.tenant_id), binding.campaign_id, binding.campaign_version))
            if (campaign is None or version is None or campaign.state != 'active'
                or campaign.current_version != binding.campaign_version):
                raise ValidationError("普通回复Campaign范围已失效")
            if row.campaign_quota_reserved_at is not None:
                return
            if row.state != 'planned' or row.lease_token != claim.lease_token or row.lease_until is None or row.lease_until <= self.now():
                raise ValidationError("普通回复不能新增额度预占")
            result = await QuotaRepositoryImpl(db, self.tenant_id).reserve_message(
                self.tenant_id, CampaignId(binding.campaign_id), self.now().date(), version.daily_total_message_limit)
            if result.status is QuotaReservationStatus.CAP_REACHED:
                raise CampaignQuotaExceededError("Campaign当日消息额度已满")
            row.campaign_quota_reserved_at = self.now()
