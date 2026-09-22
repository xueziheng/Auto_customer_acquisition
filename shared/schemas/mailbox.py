"""账号邮件镜像的技术契约；不代表需求、客户或发送授权。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

MailboxPhase = Literal["backfill", "catch_up", "synced"]


class MailboxFailure(Exception):
    """只传播固定错误，不传播 Google 原文和凭证。"""

    def __init__(self, code: str = "provider_unavailable", retry_after: int = 60):
        allowed = {
            "account_mismatch",
            "authorization_required",
            "rate_limited",
            "history_expired",
            "message_deleted",
            "provider_unavailable",
            "invalid_response",
            "configuration_invalid",
        }
        self.code = code if code in allowed else "provider_unavailable"
        self.retry_after = min(3600, max(1, retry_after))
        super().__init__(self.code)


class MailAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    filename: str = Field(max_length=2048)
    mime_type: str = Field(max_length=200)
    size: int = Field(ge=0)


class MailboxMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    message_id: str = Field(pattern=r"^[a-f0-9]{1,64}$")
    thread_id: str = Field(pattern=r"^[a-f0-9]{1,64}$")
    occurred_at: datetime
    labels: list[str]
    subject: str = Field(repr=False)
    sender: str = Field(repr=False)
    recipients: str = Field(repr=False)
    snippet: str = Field(repr=False)
    body_text: str = Field(repr=False)
    attachments: list[MailAttachment]
    source_sha256: str
    raw: dict[str, object] = Field(repr=False, exclude=True)


class MailboxPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    cursor: str = Field(repr=False, max_length=32768)
    phase: MailboxPhase
    messages: list[MailboxMessage] = Field(default_factory=list, repr=False)
    deleted_ids: list[str] = Field(default_factory=list, repr=False)
    reset: bool = False
    full_scan_complete: bool = False


class MailboxProvider(Protocol):
    async def get(self, path: str, params: dict[str, str]) -> dict[str, object]:
        """固定 Gmail 只读路径；凭证与原始异常不返回。"""
        ...
