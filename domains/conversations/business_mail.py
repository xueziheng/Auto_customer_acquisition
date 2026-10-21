"""普通业务来信：先保存事实，再追加分类推断；不晋升客户、需求或商机。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId

MailCategory = Literal["business", "other", "review"]
MailBucket = Literal["all", "pending", "business", "other", "review"]


class MailDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class BusinessMailActor(MailDTO):
    tenant_id: TenantId
    employee_id: EmployeeId
    user_id: UserId


class BusinessMailCorrection(MailDTO):
    expected_revision: int = Field(strict=True, ge=0)
    category: MailCategory
    note: str = Field(min_length=1, max_length=1000)


class BusinessMailItem(MailDTO):
    message_id: str
    mailbox_id: str
    mailbox_address: str
    owner_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime
    category: Literal["pending", "business", "other", "review"]
    classification_status: Literal["pending", "classifying", "classified", "failed"]
    revision: int
    contact_status: Literal["unconfirmed"] = "unconfirmed"
    failure_code: str | None


class BusinessMailPage(MailDTO):
    items: list[BusinessMailItem]
    next_offset: int | None


class BusinessMailSource(MailDTO):
    provider: Literal["qq"] = "qq"
    folder: Literal["INBOX"] = "INBOX"
    uidvalidity: int
    uid: int
    mime_sha256: str | None
    source_sha256: str
    source_kind: Literal["full_mime", "headers_only"]
    artifact_id: str
    received_at: datetime


class BusinessMailClassification(MailDTO):
    classification_id: str
    category: MailCategory
    origin: Literal["model", "human", "system"]
    reason: str
    evidence_quote: str | None
    model: str | None
    configuration_version: str | None
    actor_id: str | None
    created_at: datetime


class BusinessMailDetail(BusinessMailItem):
    body_text: str
    source: BusinessMailSource
    classification_history: list[BusinessMailClassification]


class BusinessMailConflict(ValidationError):
    def __init__(self) -> None:
        super().__init__("邮件归类已变化，请刷新后重新确认")


class BusinessMailResult(MailDTO):
    category: MailCategory
    reason: str = Field(min_length=1, max_length=1000)
    evidence_quote: str = Field(min_length=1, max_length=1000)


def classify_result(text: str, *, subject: str, body: str) -> BusinessMailResult:
    """只接受固定枚举及原文引句；拒绝概率、客户确认和任意额外模型控制字段。"""
    result = BusinessMailResult.model_validate_json(text)
    if (
        not result.evidence_quote.strip()
        or result.evidence_quote not in subject + "\n" + body
    ):
        raise ValueError("分类证据必须来自邮件原文")
    return result


class BusinessMailRepository(Protocol):
    async def list(
        self, actor: BusinessMailActor, *, bucket: MailBucket, offset: int, limit: int
    ) -> BusinessMailPage: ...
    async def detail(
        self, actor: BusinessMailActor, message_id: str
    ) -> BusinessMailDetail: ...
    async def correct(
        self, actor: BusinessMailActor, message_id: str, command: BusinessMailCorrection
    ) -> BusinessMailDetail: ...


class BusinessMailService:
    """公开读写入口；仓储必须同事务重核当前员工和邮箱操作版本。"""

    def __init__(self, repository: BusinessMailRepository) -> None:
        self.repository = repository

    async def list(
        self,
        actor: BusinessMailActor,
        *,
        bucket: MailBucket = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> BusinessMailPage:
        if (
            bucket not in {"all", "pending", "business", "other", "review"}
            or offset < 0
            or not 1 <= limit <= 100
        ):
            raise ValidationError("业务来信筛选无效")
        return await self.repository.list(
            actor, bucket=bucket, offset=offset, limit=limit
        )

    async def detail(
        self, actor: BusinessMailActor, message_id: str
    ) -> BusinessMailDetail:
        return await self.repository.detail(actor, message_id)

    async def correct(
        self, actor: BusinessMailActor, message_id: str, command: BusinessMailCorrection
    ) -> BusinessMailDetail:
        if not command.note.strip():
            raise ValidationError("请说明纠正原因")
        return await self.repository.correct(actor, message_id, command)
