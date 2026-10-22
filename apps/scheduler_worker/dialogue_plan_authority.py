"""规划模型只认当前执行行与批准owner，不接受模型或客户端自报run。"""
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.assistant.schemas import AssistantActor
from domains.assistant.service import ModelConfigurationService
from infra.db.dialogue_execution_tables import DialogueExecutionRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import MessageId, TenantId
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError
from workflows.reply_qualification.dialogue_authority import (
    CurrentOrdinaryDialogueAccess,
)


class DialoguePlanAuthority:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant: TenantId,
                 access: CurrentOrdinaryDialogueAccess, configuration: ModelConfigurationService,
                 *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.sessions, self.tenant, self.access, self.configuration = sessions, tenant, access, configuration
        self.now = now

    async def check(self, identity: InvocationIdentity) -> None:
        if (identity.tenant_id != self.tenant or identity.capability != 'ordinary_dialogue'
            or identity.turn_id is not None or identity.sequence != 0):
            raise ModelGenerationError('permission')
        async with self.sessions() as db:
            row = await db.scalar(select(DialogueExecutionRow).where(DialogueExecutionRow.tenant_id==self.tenant,
                DialogueExecutionRow.run_id==identity.run_id))
            if row is None or row.state != 'model_started' or row.model_sequence != identity.sequence or row.lease_until is None or row.lease_until <= self.now():
                raise ModelGenerationError('permission')
            message = MessageId(row.source_message_id)
        try:
            current = await self.access.current(self.tenant, message, identity.employee_id)
            if current.owner.user_id != identity.user_id:
                raise ModelGenerationError('permission')
            await self.configuration.authorize(AssistantActor(tenant_id=self.tenant,
                employee_id=identity.employee_id, user_id=identity.user_id), identity.configuration_version, probe=False)
        except (PermissionDenied, ValidationError):
            raise ModelGenerationError('permission') from None
