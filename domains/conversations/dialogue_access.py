"""普通对话的窄授权读缝；来源关联和邮箱资格不替代精确批准scope。"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domains.conversations.dialogue_source import DialogueSource
from domains.conversations.inbox_access import InboxActor
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId, UserId


@dataclass(frozen=True)
class DialogueOwnerSnapshot:
    source: DialogueSource
    actor: InboxActor
    user_id: UserId
    latest_message_id: MessageId
    binding_state: str
    rounds_sent: int
    model_calls: int
    no_progress_rounds: int
    pending_inbound: bool = False


class DialogueOwnerAccess(Protocol):
    async def require(
        self, tenant_id: TenantId, message_id: MessageId, employee_id: EmployeeId
    ) -> DialogueOwnerSnapshot:
        """重核当前客户owner与本人邮箱原件权限交集，不能以boss/system越权。"""
        ...


class OrdinaryDialogueAccess(Protocol):
    async def require(
        self, tenant_id: TenantId, message_id: MessageId, employee_id: EmployeeId
    ) -> InboxActor:
        """在owner原件交集上再重核当前精确获批对话范围。"""
        ...


@runtime_checkable
class OrdinaryDialogueOriginalAccess(Protocol):
    async def require_original(self, tenant_id: TenantId, message_id: MessageId,
                               employee_id: EmployeeId) -> InboxActor:
        """真人只读/接管仍需当前owner与原邮箱交集；不要求自动对话仍获批。"""
        ...
