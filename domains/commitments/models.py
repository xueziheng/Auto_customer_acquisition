"""承诺域实体。（浅域，但账本模型写全——设计稿把它列为一等对象，
且建模成本很低）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import (
    CommitmentId,
    EmployeeId,
    MessageId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
)


class CommitmentType(str, Enum):
    EMPLOYEE = "employee"
    """我方承诺。逾期是我方失信，升级路径：本人 → 经理。"""

    CUSTOMER = "customer"
    """客户承诺。到期提醒负责员工跟进——客户不会自己想起来。"""


class CommitmentStatus(str, Enum):
    PENDING = "pending"
    WAITING_CUSTOMER = "waiting_customer"
    FULFILLED = "fulfilled"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


@dataclass
class Commitment:
    """一条承诺。

    字段：
        commitment_id, tenant_id
        commitment_type
        owner:            员工承诺的承诺人；客户承诺的负责跟进员工
        account_id, opportunity_id
        action:           承诺内容（"发送更新报价" / "确认最终数量"）
        due_at:           **绝对时间**。相对表述（"tomorrow"/"Friday"）
                          必须在提取时用消息时间戳 + 客户时区解析——
                          存 "tomorrow" 一周后毫无意义
        due_at_uncertain: 解析没把握时置 True 并让员工确认。
                          错误的精确比诚实的模糊更有害：提醒会在
                          错误的日子触发
        source_message_id: 说这句话的消息（Provenance）
        verbatim:         原话摘录
        status
        extracted_by:     提取的模型版本
        confirmed_by:     确认的员工。**未确认不进提醒管道**——
                          模型会把客套话当承诺
        confirmed_at:     员工首次确认时间，与确认人一起构成修改留痕
        created_at, fulfilled_at, escalated_at
    """

    commitment_id: CommitmentId
    tenant_id: TenantId
    commitment_type: CommitmentType
    owner: EmployeeId
    action: str
    due_at: datetime
    source_message_id: MessageId
    verbatim: str
    created_at: datetime
    status: CommitmentStatus = CommitmentStatus.PENDING
    due_at_uncertain: bool = False
    account_id: ProspectAccountId | None = None
    opportunity_id: OpportunityId | None = None
    extracted_by: str | None = None
    confirmed_by: EmployeeId | None = None
    confirmed_at: datetime | None = None
    fulfilled_at: datetime | None = None
    escalated_at: datetime | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_by is not None and self.confirmed_at is not None

    def is_overdue_at(self, now: datetime) -> bool:
        """是否逾期。``due_at_uncertain`` 的承诺不判逾期——
        先让员工确认时间。"""
        open_statuses = {
            CommitmentStatus.PENDING,
            CommitmentStatus.WAITING_CUSTOMER,
            CommitmentStatus.OVERDUE,
        }
        return (
            self.is_confirmed
            and not self.due_at_uncertain
            and self.status in open_statuses
            and now >= self.due_at
        )
