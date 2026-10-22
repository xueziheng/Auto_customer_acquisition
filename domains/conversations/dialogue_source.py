"""普通对话的可追溯来源；首次询盘不伪造冷发送记录。"""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from shared.schemas.identifiers import (
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    MessageId,
    NeedHypothesisId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
)


@dataclass(frozen=True)
class DialogueSource:
    """只从同企业持久来源解析，不能由客户内容或模型提供关联。"""

    tenant_id: TenantId
    binding_id: str
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    hypothesis_id: NeedHypothesisId
    sending_identity_id: SendingIdentityId
    campaign_id: str
    campaign_version: int
    approval_id: str
    owner_employee_id: EmployeeId
    ownership_locked_at: datetime
    mailbox_operation_id: str
    source_kind: Literal["outreach", "inquiry"]
    enrollment_id: EnrollmentId | None
    business_message_id: str | None
    admitted_message_id: MessageId
    admitted_source_sha256: str


class DialogueSourceReader(Protocol):
    """只读关联不授予内容访问权限，调用方仍取当前owner与原件权限交集。"""

    async def for_message(self, tenant_id: TenantId, message_id: MessageId) -> DialogueSource | None: ...

    async def for_sent_header(self, tenant_id: TenantId, header: str) -> DialogueSource | None: ...
