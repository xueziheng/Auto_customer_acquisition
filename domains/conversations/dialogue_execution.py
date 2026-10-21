"""普通对话执行契约；租约不构成业务发送许可，当前权限必须每步重读。"""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from shared.errors import ValidationError
from shared.schemas.identifiers import MessageId, TenantId


class DialogueExecutionState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    MODEL_STARTED = "model_started"
    PLANNED = "planned"
    SENDING = "sending"
    SENT = "sent"
    REVIEW = "review"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


_TRANSITIONS = {
    DialogueExecutionState.CLAIMED: {DialogueExecutionState.MODEL_STARTED, DialogueExecutionState.REVIEW, DialogueExecutionState.CANCELLED},
    DialogueExecutionState.MODEL_STARTED: {DialogueExecutionState.PLANNED, DialogueExecutionState.UNKNOWN, DialogueExecutionState.REVIEW, DialogueExecutionState.CANCELLED},
    DialogueExecutionState.PLANNED: {DialogueExecutionState.SENDING, DialogueExecutionState.REVIEW, DialogueExecutionState.CANCELLED},
    DialogueExecutionState.SENDING: {DialogueExecutionState.SENT, DialogueExecutionState.UNKNOWN},
    DialogueExecutionState.UNKNOWN: {DialogueExecutionState.SENT},
}


def require_dialogue_transition(before: DialogueExecutionState, after: DialogueExecutionState) -> None:
    """外调开始后不能回到可派发状态；未知只能由精确发送对账确认成功。"""
    if after not in _TRANSITIONS.get(before, set()):
        raise ValidationError("普通对话执行状态不可转换")


@dataclass(frozen=True)
class DialogueExecutionClaim:
    """耐久身份与元数据，不含客户正文、邮箱或模型计划。"""

    tenant_id: TenantId
    source_message_id: MessageId
    binding_id: str
    run_id: str
    model_sequence: int
    state: DialogueExecutionState
    version: int
    lease_token: str
    lease_until: datetime
    plan_artifact_id: str | None = None
    plan_sha256: str | None = None
    attempt_id: str | None = None
    deterministic_message_id: str | None = None
    idempotency_key: str | None = None
    provider_ref: str | None = None
    need_progress_sha256: str | None = None
