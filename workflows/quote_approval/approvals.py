"""审批持久事实与报价公开契约的严格等值转换，不复制授权规则。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from domains.approvals.schemas import (
    ApprovalAccessResult,
    ApprovalFactView,
    ApprovalQuoteSubject,
)
from domains.approvals.service import ApprovalService
from domains.quotations.errors import QuoteApprovalError
from domains.quotations.schemas import QuoteApprovalFact, QuoteApprovalSubject
from domains.quotations.service import (
    QuotationVersionService,
    parse_quote_approval_payload,
    quote_change_set_ref,
)
from shared.schemas.identifiers import ApprovalId, EmployeeId, TenantId


def _subject(fact: ApprovalFactView) -> ApprovalQuoteSubject:
    """严格namespace及全部关联必须同时吻合，不将旧quote_send当新版。"""
    payload = parse_quote_approval_payload(fact.proposed_change)
    if (
        fact.contract_namespace != "quote-approval-v1"
        or fact.request_hash is None
        or fact.expires_at_limit is None
        or fact.tenant_id != payload.tenant_id
        or fact.approval_type != payload.approval_type
        or fact.proposed_by_employee != payload.prepared_by
        or fact.owner_employee != payload.submitted_owner_id
        or fact.change_set_ref
        != quote_change_set_ref(
            payload.quote_id, payload.content_hash, payload.approval_type
        )
    ):
        raise QuoteApprovalError("quote_contract_invalid")
    return ApprovalQuoteSubject(
        tenant_id=fact.tenant_id,
        approval_id=fact.approval_id,
        approval_type=payload.approval_type,
        change_set_ref=fact.change_set_ref,
        quote_id=payload.quote_id,
        quote_version=payload.quote_version,
        content_hash=payload.content_hash,
        opportunity_id=payload.opportunity_id,
        prepared_by=payload.prepared_by,
        submitted_owner_id=payload.submitted_owner_id,
    )


class QuotationApprovalAccess:
    """审批域只持本地subject，guard调用报价唯一授权规则。"""

    def __init__(self, quotations: QuotationVersionService) -> None:
        """报价公共依赖必填，不提供兼容性allow。"""
        self._quotes = quotations

    def subject(self, fact: ApprovalFactView) -> ApprovalQuoteSubject:
        """纯解析全载荷后等值投影安全身份。"""
        return _subject(fact)

    @asynccontextmanager
    async def guard(
        self,
        subject: ApprovalQuoteSubject,
        *,
        actor_id: EmployeeId,
        action: Literal["read", "decide"],
    ) -> AsyncIterator[ApprovalAccessResult]:
        """当前角色从真实报价lease返回，不由请求声明填充。"""
        mapped = QuoteApprovalSubject(
            tenant_id=subject.tenant_id,
            approval_id=subject.approval_id,
            approval_type=subject.approval_type,
            quote_id=subject.quote_id,
            quote_version=subject.quote_version,
            content_hash=subject.content_hash,
            opportunity_id=subject.opportunity_id,
            prepared_by=subject.prepared_by,
            submitted_owner_id=subject.submitted_owner_id,
        )
        async with self._quotes.open_approval_access(
            subject.tenant_id, mapped, actor_id=actor_id, action=action
        ) as result:
            yield ApprovalAccessResult(
                can_decide=result.can_decide, current_role=result.current_role
            )


async def read_quote_facts(
    approvals: ApprovalService,
    tenant_id: TenantId,
    approval_ids: tuple[ApprovalId, ...],
) -> tuple[QuoteApprovalFact, ...]:
    """每次读真实包；事件只唤醒，不提供决定或应用成功事实。"""
    result = []
    for approval_id in approval_ids:
        fact = await approvals.read_fact(tenant_id, approval_id)
        subject = _subject(fact)
        if subject.tenant_id != tenant_id or subject.approval_id != approval_id:
            raise QuoteApprovalError("approval_fact_invalid")
        state = fact.state.value
        decision = (
            "approve"
            if state in {"approved", "applied", "apply_failed"}
            else "reject"
            if state == "rejected"
            else None
        )
        if (
            decision is None
            and (fact.decided_by_employee is not None or fact.decided_at is not None)
        ) or (
            decision is not None
            and (fact.decided_by_employee is None or fact.decided_at is None)
        ):
            raise QuoteApprovalError("approval_fact_invalid")
        result.append(
            QuoteApprovalFact(
                tenant_id=fact.tenant_id,
                approval_id=approval_id,
                approval_type=subject.approval_type,
                change_set_ref=subject.change_set_ref,
                request_hash=fact.request_hash,
                payload=parse_quote_approval_payload(fact.proposed_change),
                created_at=fact.created_at,
                expires_at=fact.expires_at,
                expires_at_limit=fact.expires_at_limit,
                prepared_by=subject.prepared_by,
                submitted_owner_id=subject.submitted_owner_id,
                proposed_by_run=fact.proposed_by_run,
                state=state,
                decision=decision,
                decided_by=fact.decided_by_employee,
                decided_at=fact.decided_at,
                decision_note=fact.decision_note,
                applied_at=fact.applied_at,
                application_error_code=fact.application_error_code,
            )
        )
    return tuple(result)
