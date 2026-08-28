"""审批域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from domains.approvals.models import ApprovalState, BlastRadius
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    OpportunityId,
    QuoteId,
    RunId,
    TenantId,
)
from shared.schemas.quote_creation import QuoteDTO, QuoteTime
from shared.schemas.quote_facts import FactHash


class ApprovalFactView(QuoteDTO):
    """受信workflow专用持久事实，不注册HTTP响应模型。"""

    tenant_id: TenantId
    approval_id: ApprovalId
    approval_type: str
    state: ApprovalState
    title: str
    proposed_change: dict[str, JsonValue]
    reason: str
    blast_radius: BlastRadius
    proposed_by_run: RunId | None
    proposed_by_employee: EmployeeId | None
    owner_employee: EmployeeId | None
    evidence_refs: tuple[str, ...]
    change_set_ref: str | None
    created_at: QuoteTime
    expires_at: QuoteTime
    expires_at_limit: QuoteTime | None
    request_hash: FactHash | None
    contract_namespace: Literal["quote-approval-v1"] | None
    decided_by_employee: EmployeeId | None
    decided_at: QuoteTime | None
    decision_note: str | None
    applied_at: QuoteTime | None
    application_error_code: str | None


class ApprovalQuoteSubject(QuoteDTO):
    """审批域本地最小对象，不导入报价域类型。"""

    tenant_id: TenantId
    approval_id: ApprovalId | None
    approval_type: str
    change_set_ref: str
    quote_id: QuoteId
    quote_version: int
    content_hash: FactHash
    opportunity_id: OpportunityId
    prepared_by: EmployeeId
    submitted_owner_id: EmployeeId


class ApprovalAccessResult(QuoteDTO):
    """当前lease中的事实，不作为可缓存授权token。"""

    can_decide: bool
    current_role: Literal[
        "boss", "manager", "sales", "sourcing", "product", "finance", "viewer"
    ]


class ApprovalReaderIdentity(QuoteDTO):
    """来自可信RequestIdentity，当前role仍由guard二次核对。"""

    employee_id: EmployeeId
    role: Literal[
        "boss", "manager", "sales", "sourcing", "product", "finance", "viewer"
    ]


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
