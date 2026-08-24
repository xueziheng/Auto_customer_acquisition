"""审批域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shared.schemas.identifiers import EmployeeId


@dataclass(frozen=True)
class ApprovalView:
    """审批视图 —— 审批人看到的全部内容。

    设计目标：打开 → 看完 → 决定，一分钟内。字段齐全到不需要
    跳转任何其他页面。
    """

    approval_id: str
    approval_type: str
    type_label: str
    title: str
    reason: str
    proposed_change_display: dict[str, str]
    affected_entities: list[str]
    if_approved: str
    if_rejected: str
    reversible: bool
    state: str
    created_at: datetime
    expires_at: datetime
    proposed_by: str | None = None
    evidence_links: list[str] = field(default_factory=list)
    owner_name: str | None = None
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    seconds_until_expiry: int | None = None
    can_current_user_decide: bool = False
    """当前用户能否决定（含自批禁止判断）。由服务层算好传给前端，
    不让前端自己判断——前端判断会漏。"""
    change_set_ref: str | None = None
    decided_by_employee: EmployeeId | None = None
    applied_at: datetime | None = None
    application_error_code: str | None = None
