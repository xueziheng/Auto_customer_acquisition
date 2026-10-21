"""回复组合的本进程借用资源与一次性客户证据绑定；不拥有连接池或原件。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import BoundedRawArtifactStore, GeneratedArtifactStore
from domains.conversations.dialogue_access import (
    OrdinaryDialogueAccess,
    OrdinaryDialogueOriginalAccess,
)
from domains.conversations.inbox_access import InboxActor
from domains.demand.schemas import (
    CustomerReplyEvidenceClaim,
    VerifiedCustomerReplyEvidence,
)
from domains.demand.service import CustomerReplyEvidenceVerifier
from domains.opportunities.service import OpportunityService
from infra.db.qq_trial import SqlQQTrialGate
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId


@dataclass(frozen=True)
class ReplyRuntimeResources:
    """仅借用原runtime资源，关闭仍归原inbound与engine owner。"""

    sessions: async_sessionmaker[AsyncSession]
    bounded_raw_store: BoundedRawArtifactStore
    opportunities: OpportunityService
    now: Callable[[], datetime]
    trial_gate: SqlQQTrialGate | None = None
    bind_classification_commit: Callable[[Callable[[AsyncSession, MessageId | None], Awaitable[None]]], None] | None = None

    bind_dialogue_access: Callable[[OrdinaryDialogueAccess], None] | None = None
    generated_store: GeneratedArtifactStore | None = None


class DeferredOrdinaryDialogueAccess:
    """规范Conversation服务的一次性普通对话授权槽，未装配默认拒绝。"""
    def __init__(self) -> None:
        self.delegate: OrdinaryDialogueAccess | None = None

    def bind(self, delegate: OrdinaryDialogueAccess) -> None:
        if self.delegate is not None:
            raise ValidationError("普通对话授权只能装配一次")
        self.delegate = delegate

    async def require_original(self, tenant_id: TenantId, message_id: MessageId, employee_id: EmployeeId) -> InboxActor:
        if not isinstance(self.delegate, OrdinaryDialogueOriginalAccess):
            raise ValidationError("普通对话原件授权未配置")
        return await self.delegate.require_original(tenant_id, message_id, employee_id)

    async def require(self, tenant_id: TenantId, message_id: MessageId, employee_id: EmployeeId) -> InboxActor:
        if self.delegate is None:
            raise ValidationError("普通对话授权未配置")
        return await self.delegate.require(tenant_id, message_id, employee_id)


class DeferredCustomerReplyEvidenceVerifier:
    """Demand构造先注入；canonical Outreach形成后且ready前只绑定一次。"""

    def __init__(self) -> None:
        self._delegate: CustomerReplyEvidenceVerifier | None = None

    def bind(self, delegate: CustomerReplyEvidenceVerifier) -> None:
        if self._delegate is not None or not isinstance(
            delegate, CustomerReplyEvidenceVerifier
        ):
            raise ValidationError("回复客户证据验证器绑定无效")
        self._delegate = delegate

    async def verify(
        self, tenant_id: TenantId, claim: CustomerReplyEvidenceClaim
    ) -> VerifiedCustomerReplyEvidence:
        if self._delegate is None:
            raise ValidationError("回复客户证据验证器未绑定")
        return await self._delegate.verify(tenant_id, claim)


class DeferredReplyCommit:
    """同一规范会话 UoW 的一次性装配槽；不更换域服务实例。"""
    def __init__(self, original: Callable[[AsyncSession, MessageId | None], Awaitable[None]] | None) -> None:
        self._original = original
        self._bound: Callable[[AsyncSession, MessageId | None], Awaitable[None]] | None = None

    def bind(self, callback: Callable[[AsyncSession, MessageId | None], Awaitable[None]]) -> None:
        if self._bound is not None or not callable(callback):
            raise ValueError("回复提交门禁只能装配一次")
        self._bound = callback

    async def commit(self, session: AsyncSession, message: MessageId | None) -> None:
        callback = self._bound or self._original
        if callback is not None:
            await callback(session, message)
