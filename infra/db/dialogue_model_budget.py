"""有界provider轮预占；未知不退款，持久调用身份重放也不再次派发。"""
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.dialogue_source import DialogueSource
from infra.db.dialogue_execution_tables import (
    DialogueBindingRow,
    DialogueModelReservationRow,
)
from infra.db.dialogue_source import read_dialogue_source
from shared.schemas.identifiers import MessageId, TenantId
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError


class SqlDialogueModelBudget:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant_id: TenantId) -> None:
        self.sessions, self.tenant_id = sessions, tenant_id

    async def reserve(self, source: DialogueSource, identity: InvocationIdentity, *, provider_round: int,
                      model: str, maximum_calls: int, message_id: MessageId | None = None) -> None:
        """调用方先验精确批准上限；本端口同事务CAS总量与稳定provider轮身份。"""
        message = message_id or source.admitted_message_id
        if (identity.tenant_id != self.tenant_id or source.tenant_id != self.tenant_id
            or identity.employee_id != source.owner_employee_id or identity.turn_id is not None
            or identity.capability not in {'reply_qualification', 'ordinary_dialogue'}
            or type(maximum_calls) is not int or maximum_calls < 1
            or type(provider_round) is not int or provider_round < 0):
            raise ModelGenerationError('permission')
        async with self.sessions.begin() as db:
            binding = await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id == self.tenant_id,
                DialogueBindingRow.binding_id == source.binding_id).with_for_update())
            if (binding is None or binding.state != 'active' or binding.latest_message_id != message
                or await read_dialogue_source(db, self.tenant_id, message) != source):
                raise ModelGenerationError('permission')
            if binding.model_calls >= maximum_calls:
                raise ModelGenerationError('quota')
            key = (str(self.tenant_id), str(identity.run_id), identity.capability, identity.sequence, provider_round)
            if await db.get(DialogueModelReservationRow, key) is not None:
                raise ModelGenerationError('permission')
            db.add(DialogueModelReservationRow(tenant_id=self.tenant_id, binding_id=source.binding_id,
                source_message_id=message, run_id=identity.run_id, capability=identity.capability,
                sequence=identity.sequence, provider_round=provider_round, employee_id=identity.employee_id,
                user_id=identity.user_id, configuration_version=identity.configuration_version, model=model,
                reserved_at=datetime.now(UTC)))
            binding.model_calls += 1
