"""人工恢复只授予一次规范 classify 推进，不授予发信或新建业务成果。"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from domains.conversations.service import InboxActor
from shared.errors import ValidationError
from shared.schemas.identifiers import MessageId, TenantId


@dataclass(frozen=True)
class RetryFacts:
    run_status: str
    step: str
    invocation_count: int
    invocation_state: str
    dispatched: bool
    sequence: int
    classified: bool
    provider_requests: int
    provider_pending: bool
    configuration_matches: bool
    original_binding: bool


def require_retryable(facts: RetryFacts) -> None:
    """只有确证零派发且未产生分类的失败步骤才能进入人工恢复。"""
    if (
        facts.run_status != "failed"
        or facts.step != "classify"
        or facts.invocation_count != 1
        or facts.invocation_state != "rejected"
        or facts.dispatched
        or facts.sequence != 0
        or facts.classified
        or facts.provider_requests != 0
        or facts.provider_pending
        or not facts.configuration_matches
        or not facts.original_binding
    ):
        raise ValidationError("该回信不符合未派发失败的恢复条件")


class ReplyRetryView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: str
    can_retry: bool = False
    can_resume: bool = False
    resumed: bool = False
    expected_sequence: int | None = None
    requires_cost_confirmation: bool = False
    state: Literal[
        "unavailable",
        "eligible",
        "queued",
        "running",
        "classified",
        "completed",
        "failed",
        "expired",
    ]
    reason: Literal[
        "not_recoverable",
        "not_dispatched",
        "quota_exhausted",
        "model_unavailable",
        "manual_recovery",
        "stopped_failure",
    ]
    original_failure_preserved: bool = True


class ReplyRetryCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReplyFailedRetryCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_sequence: int = Field(strict=True, ge=0)
    acknowledge_cost: bool = Field(strict=True)


class ReplyRetryRepository(Protocol):
    async def read(
        self, tenant: TenantId, message: MessageId, actor: InboxActor
    ) -> ReplyRetryView: ...
    async def request(
        self, tenant: TenantId, message: MessageId, actor: InboxActor, key: str
    ) -> ReplyRetryView: ...

    async def retry_failed(
        self,
        tenant: TenantId,
        message: MessageId,
        actor: InboxActor,
        key: str,
        command: ReplyFailedRetryCommand,
    ) -> ReplyRetryView: ...

    async def resume(
        self, tenant: TenantId, message: MessageId, actor: InboxActor, key: str
    ) -> ReplyRetryView: ...


class ReplyRetryApplication:
    def __init__(self, repository: ReplyRetryRepository) -> None:
        self.repository = repository

    async def read(
        self, tenant: TenantId, message: MessageId, actor: InboxActor
    ) -> ReplyRetryView:
        return await self.repository.read(tenant, message, actor)

    async def request(
        self, tenant: TenantId, message: MessageId, actor: InboxActor, key: str
    ) -> ReplyRetryView:
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or key != key.strip():
            raise ValidationError("重新识别需要原始幂等键")
        return await self.repository.request(tenant, message, actor, key)

    async def retry_failed(
        self,
        tenant: TenantId,
        message: MessageId,
        actor: InboxActor,
        key: str,
        command: ReplyFailedRetryCommand,
    ) -> ReplyRetryView:
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or key != key.strip():
            raise ValidationError("重新识别需要原始幂等键")
        return await self.repository.retry_failed(tenant, message, actor, key, command)

    async def resume(
        self, tenant: TenantId, message: MessageId, actor: InboxActor, key: str
    ) -> ReplyRetryView:
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or key != key.strip():
            raise ValidationError("继续识别需要原始幂等键")
        return await self.repository.resume(tenant, message, actor, key)
