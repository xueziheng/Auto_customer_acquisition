"""普通来信人工处理与未发送草稿；不产生发送或客户资格。"""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, field_validator

from domains.conversations.business_mail import BusinessMailActor, MailDTO
from shared.errors import ValidationError

HandlingStatus = Literal["pending", "in_progress", "waiting_customer", "completed"]


class BusinessMailReplyDraftView(MailDTO):
    draft_id: str
    subject: str
    body_text: str
    source_sha256: str
    created_by: str
    created_at: datetime


class BusinessMailFollowupEvent(MailDTO):
    event_id: str
    request_key: str
    kind: Literal["draft_saved", "handling_updated"]
    revision: int
    status: HandlingStatus
    note: str
    actor_id: str
    created_at: datetime
    draft_id: str | None


class BusinessMailFollowupView(MailDTO):
    message_id: str
    revision: int
    owner_id: str
    status: HandlingStatus
    draft: BusinessMailReplyDraftView | None
    history: list[BusinessMailFollowupEvent]


class BusinessMailDraftCommand(MailDTO):
    expected_revision: int = Field(strict=True, ge=0)
    subject: str = Field(strict=True, min_length=1, max_length=500)
    body_text: str = Field(strict=True, min_length=1, max_length=10000)

    @field_validator("subject", "body_text", mode="before")
    @classmethod
    def clean(cls, value: object, info):
        if not isinstance(value, str):
            raise ValueError("草稿文本无效")  # noqa: TRY004 — 保持Pydantic脱敏验证错误
        if (
            any(
                ord(c) < 32 and (info.field_name == "subject" or c not in "\n\t")
                for c in value
            )
            or "\x7f" in value
        ):
            raise ValueError("草稿文本包含控制字符")
        return value.strip()


class BusinessMailHandlingCommand(MailDTO):
    expected_revision: int = Field(strict=True, ge=0)
    status: Literal["in_progress", "waiting_customer", "completed"]
    note: str = Field(strict=True, min_length=1, max_length=2000)

    @field_validator("note", mode="before")
    @classmethod
    def clean(cls, value: object):
        if (
            not isinstance(value, str)
            or any(ord(c) < 32 and c not in "\n\t" for c in value)
            or "\x7f" in value
        ):
            raise ValueError("处理说明无效")
        return value.strip()


class BusinessMailFollowupConflict(ValidationError):
    def __init__(self) -> None:
        super().__init__("邮件处理记录已变化，请刷新后重新确认")


def validate_idempotency_key(key: str) -> str:
    if (
        not isinstance(key, str)
        or not 8 <= len(key) <= 200
        or any(not 33 <= ord(c) <= 126 for c in key)
    ):
        raise ValueError("请求幂等键无效")
    return key


class BusinessMailFollowupRepository(Protocol):
    async def get(
        self, actor: BusinessMailActor, message_id: str
    ) -> BusinessMailFollowupView: ...
    async def apply(
        self,
        actor: BusinessMailActor,
        message_id: str,
        command: BusinessMailDraftCommand | BusinessMailHandlingCommand,
        *,
        idempotency_key: str,
    ) -> BusinessMailFollowupView: ...


class BusinessMailFollowupService:
    """独立服务；每次读取与重放仍由仓储重核当前授权。"""

    def __init__(self, repository: BusinessMailFollowupRepository) -> None:
        self.repository = repository

    async def get(
        self, actor: BusinessMailActor, message_id: str
    ) -> BusinessMailFollowupView:
        return await self.repository.get(actor, message_id)

    async def save_draft(
        self,
        actor: BusinessMailActor,
        message_id: str,
        command: BusinessMailDraftCommand,
        *,
        idempotency_key: str,
    ) -> BusinessMailFollowupView:
        return await self.repository.apply(
            actor,
            message_id,
            command,
            idempotency_key=validate_idempotency_key(idempotency_key),
        )

    async def handle(
        self,
        actor: BusinessMailActor,
        message_id: str,
        command: BusinessMailHandlingCommand,
        *,
        idempotency_key: str,
    ) -> BusinessMailFollowupView:
        return await self.repository.apply(
            actor,
            message_id,
            command,
            idempotency_key=validate_idempotency_key(idempotency_key),
        )
