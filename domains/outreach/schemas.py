"""触达域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SequenceStepRequest:
    step_number: int
    intent: str
    wait_days: int


@dataclass(frozen=True)
class CampaignCreateRequest:
    """创建 Campaign 的入参。字段与 ``CampaignBoundary`` 对应。"""

    name: str
    markets: list[str]
    target_entity_types: list[str]
    allowed_categories: list[str]
    sender_identity_ids: list[str]
    steps: list[SequenceStepRequest]
    daily_new_contacts: int
    daily_total_messages: int
    handoff_triggers: list[str]
    stop_on_reply: bool = True


@dataclass(frozen=True)
class CampaignView:
    """Campaign 视图。

    ``today_usage`` 让老板一眼看到「今天发了多少 / 上限多少」——
    这是他批准边界时最关心的数。
    """

    campaign_id: str
    name: str
    state: str
    version: int
    markets: list[str]
    target_entity_types: list[str]
    allowed_categories: list[str]
    sender_identities: list[str]
    max_messages: int
    stop_on_reply: bool
    daily_new_contacts: int
    daily_total_messages: int
    handoff_triggers: list[str]
    created_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    paused_reason: str | None = None
    today_new_contacts_used: int = 0
    today_total_messages_used: int = 0
    active_enrollments: int = 0
    replied_count: int = 0


@dataclass(frozen=True)
class EnrollmentView:
    enrollment_id: str
    campaign_id: str
    account_id: str
    account_name: str
    contact_point_id: str
    state: str
    current_step: int
    max_steps: int
    enrolled_at: datetime
    next_send_at: datetime | None = None
    stopped_reason: str | None = None


@dataclass(frozen=True)
class SendAuthorization:
    """发送授权 —— ``prepare_send`` 的结果。

    ``tool_gateway`` 执行发送时消费它。

    字段：
        authorized:        是否放行
        enrollment_id
        step_number, step_intent
        sending_identity_id
        idempotency_key:   幂等键（{tenant}:{enrollment}:{step}），
                           防止 scheduler 重扫导致同一步发两次
        denial_reason:     未放行的原因（人类可读，会进 Run 记录）
    """

    authorized: bool
    enrollment_id: str
    step_number: int
    step_intent: str
    sending_identity_id: str
    idempotency_key: str
    denial_reason: str | None = None
