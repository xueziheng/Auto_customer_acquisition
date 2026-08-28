"""报价审批唯一业务规则：安全白名单、版本化hash与当前ABAC。"""

import json
import re
from datetime import datetime
from typing import Literal, cast

from pydantic import JsonValue

from domains.quotations.approval_schemas import (
    QuoteApprovalAccessContext,
    QuoteApprovalApplicationReceipt,
    QuoteApprovalCalculationSummary,
    QuoteApprovalCustomerSummary,
    QuoteApprovalDecisionSnapshot,
    QuoteApprovalEvidenceSummary,
    QuoteApprovalFact,
    QuoteApprovalFxSummary,
    QuoteApprovalPackagePayload,
    QuoteApprovalPolicySummary,
    QuoteApprovalPreviousSummary,
    QuoteApprovalSnapshot,
    QuoteApprovalSubject,
    QuoteApprovalType,
    QuoteWorkflowRunFact,
)
from domains.quotations.basis_schemas import Hash, QuotePolicySnapshot
from domains.quotations.content import validate_quote_basis
from domains.quotations.context import QuoteBusinessContext
from domains.quotations.errors import (
    QuotationError,
    QuoteApprovalError,
    QuoteApprovalPermissionError,
    QuoteApprovalUnavailableError,
)
from domains.quotations.version_schemas import QuoteDetailView
from shared.errors import PermissionDenied
from shared.schemas.identifiers import QuoteId, RunId, TenantId
from shared.schemas.quote_creation import canonical_creation_hash
from shared.schemas.quote_facts import QuoteEmployeeFact


def require_quote_current_basis(quote: QuoteDetailView, context: QuoteBusinessContext,
    policy: QuotePolicySnapshot, *, now: datetime) -> None:
    """T5与正式文件共用当前政策/完整context/第二道依据门，不重价。"""
    c = quote.content
    if (policy.policy_id, policy.content_hash) != (c.basis.policy_id, c.basis.policy.content_hash):
        raise QuoteApprovalError("policy_stale")
    if context.context_hash != c.basis.context_hash or context.opportunity_state in {"won", "lost", "disqualified"}:
        raise QuoteApprovalError("context_changed")
    if c.valid_until <= now:
        raise QuoteApprovalError("approval_expired")
    try:
        validate_quote_basis(c.intent, c.basis, context, now=now)
    except QuotationError as error:
        code = error.code if error.code in {"context_changed", "evidence_invalid", "evidence_expired"} else "evidence_invalid"
        raise QuoteApprovalError(code) from None


def require_quote_decision_states(facts: tuple[QuoteApprovalFact, ...], *, allow_applied: bool) -> None:
    """当前决定形状门；原apply在fresh前保持原错误顺序。"""
    if not allow_applied and any(f.state == "applied" for f in facts):
        raise QuoteApprovalUnavailableError("storage_inconsistent")
    states = {"approved", "applied"} if allow_applied else {"approved"}
    if any(f.state not in states or f.decision != "approve" or f.decided_by is None or f.decided_at is None for f in facts):
        raise QuoteApprovalError("approval_fact_invalid")


def require_quote_approved_decisions(facts: tuple[QuoteApprovalFact, ...], *, now: datetime, allow_applied: bool) -> None:
    """只有真实批准且期限内的决定可用；allow_applied由可信用途固定。"""
    require_quote_decision_states(facts, allow_applied=allow_applied)
    if any(now >= f.expires_at or f.decided_at is None or not f.created_at <= f.decided_at < f.expires_at for f in facts):
        raise QuoteApprovalError("approval_expired")


def require_quote_current_deciders(quote: QuoteDetailView, facts: tuple[QuoteApprovalFact, ...],
    business: QuoteBusinessContext, deciders: tuple[QuoteEmployeeFact, ...]) -> None:
    """全部决定人逐一核当前独立审批权；文件actor无需是其中之一。"""
    current = {e.employee_id: e for e in deciders}
    if len(current) != len(deciders) or set(current) != {f.decided_by for f in facts}:
        raise QuoteApprovalError("context_changed")
    c = quote.content
    for f in facts:
        if f.decided_by is None:
            raise QuoteApprovalError("context_changed")
        subject = QuoteApprovalSubject(tenant_id=c.tenant_id, approval_id=f.approval_id,
            quote_id=c.quote_id, quote_version=c.version, content_hash=c.content_hash,
            opportunity_id=c.opportunity_id, prepared_by=c.prepared_by,
            submitted_owner_id=c.owner_id, approval_type=f.approval_type)
        access = QuoteApprovalAccessContext(tenant_id=c.tenant_id, opportunity_id=c.opportunity_id,
            actor=current[f.decided_by], owner=business.runtime.owner,
            prepared_by=c.prepared_by, submitted_owner_id=c.owner_id)
        try:
            require_quote_approval_access(subject, access, action="apply")
        except PermissionDenied:
            raise QuoteApprovalError("decider_invalid") from None

APPROVAL_TYPES: tuple[QuoteApprovalType, ...] = (
    "quote_send",
    "margin_floor_override",
    "discount",
    "delivery_commitment",
    "payment_terms",
    "certification_commitment",
)


def require_quote_run_binding(
    tenant_id: TenantId, quote_id: QuoteId, quote_version: int,
    quote_content_hash: str, run: QuoteWorkflowRunFact | None,
) -> None:
    """历史与fresh入口同核真实run，不把生命周期终态当绑定失效。"""
    if run is None or (
        run.tenant_id, run.workflow_type, run.workflow_version, run.subject_ref,
        run.quote_version, run.content_hash,
    ) != (tenant_id, "quote_approval", 1, str(quote_id), quote_version, quote_content_hash):
        raise QuoteApprovalError("workflow_binding_invalid")


def build_quote_approval_snapshot(
    quote: QuoteDetailView, previous: QuoteDetailView | None,
) -> QuoteApprovalSnapshot:
    """按原报价及实际前一版恢复完整载荷与原limit；不读取fresh事实。"""
    c = quote.content
    if c.version > 1 and (previous is None or previous.content.version != c.version - 1):
        raise QuoteApprovalUnavailableError("storage_inconsistent")
    return QuoteApprovalSnapshot(
        internal_quote=quote, required_types=required_quote_approvals(quote),
        payloads=quote_approval_payloads(quote, previous),
        expires_at_limit=min(c.valid_until, c.basis.valid_until, *(
            e.valid_until for e in c.basis.price_evidence if e.valid_until is not None)),
    )


def require_quote_approval_facts(
    snapshot: QuoteApprovalSnapshot, facts: tuple[QuoteApprovalFact, ...], run_id: RunId,
) -> None:
    """完整原请求组及不可变payload/owner/期限/run校验，T5与文件历史共用。"""
    c = snapshot.internal_quote.content
    if (len(facts) != len(snapshot.required_types)
        or {f.approval_type for f in facts} != set(snapshot.required_types)
        or len({f.approval_id for f in facts}) != len(facts)):
        raise QuoteApprovalError("approval_fact_invalid")
    payloads = {p.approval_type: p for p in snapshot.payloads}
    for f in facts:
        if (
            f.tenant_id != c.tenant_id or f.payload != payloads[f.approval_type]
            or f.change_set_ref != quote_change_set_ref(c.quote_id, c.content_hash, f.approval_type)
            or f.prepared_by != c.prepared_by or f.submitted_owner_id != c.owner_id
            or f.expires_at_limit != snapshot.expires_at_limit
            or not f.created_at < f.expires_at <= f.expires_at_limit
        ):
            raise QuoteApprovalError("approval_fact_invalid")
        if f.proposed_by_run != run_id:
            raise QuoteApprovalError("workflow_binding_invalid")


def require_quote_approval_bindings(
    facts: tuple[QuoteApprovalFact, ...], bindings: tuple[QuoteApprovalFact, ...],
) -> None:
    """决定只能来自原轮相同包，逐项比较全部不可变请求字段。"""
    fields = (
        "tenant_id", "approval_id", "approval_type", "change_set_ref", "request_hash",
        "payload", "created_at", "expires_at", "expires_at_limit", "prepared_by",
        "submitted_owner_id", "proposed_by_run",
    )
    old = {f.approval_type: f for f in bindings}
    if len(old) != len(facts) or any(
        f.approval_type not in old
        or any(getattr(f, n) != getattr(old[f.approval_type], n) for n in fields)
        for f in facts
    ):
        raise QuoteApprovalError("approval_binding_conflict")


def receipt_facts(receipt: QuoteApprovalApplicationReceipt) -> tuple[QuoteApprovalFact, ...]:
    """仅用于历史验证，不冒充approvals实时状态事实。"""
    return tuple(QuoteApprovalFact(**{
        name: getattr(d, name) for name in QuoteApprovalDecisionSnapshot.model_fields
    }, state="approved", applied_at=None, application_error_code=None) for d in receipt.decisions)


def require_quote_approval_receipt(
    snapshot: QuoteApprovalSnapshot, receipt: QuoteApprovalApplicationReceipt,
    bindings: tuple[QuoteApprovalFact, ...], run_id: RunId,
) -> None:
    """真实成功归属、原组和决定hash全部验证，不重新批准历史报价。"""
    c = snapshot.internal_quote.content
    if (receipt.tenant_id, receipt.quote_id, receipt.quote_version, receipt.content_hash,
        receipt.approval_run_id) != (c.tenant_id, c.quote_id, c.version, c.content_hash, run_id):
        raise QuoteApprovalError("workflow_binding_invalid")
    facts = receipt_facts(receipt)
    require_quote_approval_facts(snapshot, facts, run_id)
    require_quote_approval_bindings(facts, bindings)
    if (
        quote_approval_facts_hash(facts) != receipt.facts_hash
        or any(f.decision != "approve" for f in facts)
        or next(f.decided_by for f in facts if f.approval_type == "quote_send")
        != receipt.quote_send_decider
    ):
        raise QuoteApprovalUnavailableError("storage_inconsistent")


def quote_change_set_ref(
    quote_id: QuoteId, content_hash: Hash, approval_type: QuoteApprovalType
) -> str:
    """精确namespace，不接受大小写、空白或半个标记。"""
    if (
        not re.fullmatch(r"quo_[0-9A-HJKMNP-TV-Z]{26}", quote_id)
        or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
        or approval_type not in APPROVAL_TYPES
    ):
        raise QuoteApprovalError("quote_contract_invalid")
    return f"quote:{quote_id}:{content_hash}:{approval_type}"


def required_quote_approvals(quote: QuoteDetailView) -> tuple[QuoteApprovalType, ...]:
    """按冻结利润和条款集合决定所需类型，不用模型概率或隐含承诺。"""
    required = {"quote_send", *(term.kind for term in quote.content.terms)}
    basis = quote.content.basis
    if basis.calculation.metrics.margin_rate < basis.policy.minimum_margin_rate:
        required.add("margin_floor_override")
    return tuple(kind for kind in APPROVAL_TYPES if kind in required)


def _customer(quote: QuoteDetailView) -> QuoteApprovalCustomerSummary:
    """逐字段白名单，禁止自动dump整份报价。"""
    c = quote.content
    return QuoteApprovalCustomerSummary(
        issuer_name=c.issuer.name,
        issuer_address=c.issuer.address,
        issuer_contact=c.issuer.contact,
        account_name=c.account_name,
        country=c.country,
        line=c.lines[0],
        valid_until=c.valid_until,
        terms=c.terms,
    )


def _calculation(quote: QuoteDetailView) -> QuoteApprovalCalculationSummary:
    """成本FX保留原顺序；独立报价FX不混用字段，不补虚构汇率。"""
    basis = quote.content.basis
    c, fx = basis.calculation, basis.quote_fx
    return QuoteApprovalCalculationSummary(
        base_currency=c.base_currency,
        quote_currency=c.quote_currency,
        effective_unit_revenue=c.effective_unit_revenue,
        displayed_unit_price=c.displayed_unit_price,
        displayed_total=c.displayed_total,
        metrics=c.metrics,
        cost_fx_rates=tuple(
            QuoteApprovalFxSummary(
                source_currency=r.base,
                target_currency=r.quote,
                rate=r.rate,
                observed_at=r.observed_at,
                reference_id=basis.cost_sheet_id,
            )
            for r in basis.cost_fx_rates
        ),
        quote_fx=None
        if fx is None
        else QuoteApprovalFxSummary(
            source_currency=fx.base_currency,
            target_currency=fx.quote_currency,
            rate=fx.rate,
            observed_at=fx.observed_at,
            reference_id=fx.fx_id,
        ),
    )


def _policy(quote: QuoteDetailView) -> QuoteApprovalPolicySummary:
    """政策只展示身份、阈值和生效时间，不携带来源。"""
    p = quote.content.basis.policy
    return QuoteApprovalPolicySummary(
        policy_id=p.policy_id,
        content_hash=p.content_hash,
        category=p.category,
        minimum_margin_rate=p.minimum_margin_rate,
        target_margin_rate=p.target_margin_rate,
        effective_from=p.effective_from,
    )


def quote_approval_payloads(
    quote: QuoteDetailView, previous: QuoteDetailView | None
) -> tuple[QuoteApprovalPackagePayload, ...]:
    """每类独立绑定同版本与完整安全载荷，不复制内部basis/Need/Provenance。"""
    c = quote.content
    required = required_quote_approvals(quote)
    historical = (
        None
        if previous is None
        else QuoteApprovalPreviousSummary(
            quote_id=previous.content.quote_id,
            version=previous.content.version,
            content_hash=previous.content.content_hash,
            customer=_customer(previous),
            calculation=_calculation(previous),
            policy=_policy(previous),
        )
    )
    evidence = tuple(
        QuoteApprovalEvidenceSummary(
            evidence_id=e.evidence_id,
            evidence_hash=e.evidence_hash,
            kind=e.kind,
            basis=cast(Literal["quoted", "actual"], e.basis),
            amount=e.amount,
            valid_until=e.valid_until,
            confirmed_by=e.confirmed_by,
            confirmed_at=e.confirmed_at,
        )
        for e in c.basis.price_evidence
    )
    result = tuple(
        QuoteApprovalPackagePayload(
            schema_version="quote-approval-v1",
            tenant_id=c.tenant_id,
            quote_id=c.quote_id,
            quote_version=c.version,
            opportunity_id=c.opportunity_id,
            content_hash=c.content_hash,
            context_hash=c.basis.context_hash,
            basis_id=c.basis.basis_id,
            basis_hash=c.basis.basis_hash,
            prepared_by=c.prepared_by,
            submitted_owner_id=c.owner_id,
            approval_type=kind,
            required_types=required,
            policy=_policy(quote),
            customer=_customer(quote),
            calculation=_calculation(quote),
            evidence=evidence,
            previous=historical,
        )
        for kind in required
    )
    for payload in result:
        _check_payload_size(payload.model_dump_json())
    return result


def _check_payload_size(value: str) -> None:
    """持久JSON资源限制与旧审批上限一致。"""
    if len(value.encode("utf-8")) > 64_000:
        raise QuoteApprovalError("invalid_input")


def parse_quote_approval_payload(
    value: dict[str, JsonValue],
) -> QuoteApprovalPackagePayload:
    """唯一JSON适配边界，严格解码Decimal字符串/时间/tuple且不删额外字段。"""
    rendered = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    _check_payload_size(rendered)
    payload = QuoteApprovalPackagePayload.model_validate_json(rendered)
    expected = tuple(kind for kind in APPROVAL_TYPES if kind in payload.required_types)
    if (
        payload.required_types != expected
        or "quote_send" not in expected
        or payload.approval_type not in expected
    ):
        raise QuoteApprovalError("quote_contract_invalid")
    quote_change_set_ref(payload.quote_id, payload.content_hash, payload.approval_type)
    return payload


def quote_approval_payload_hash(payload: QuoteApprovalPackagePayload) -> Hash:
    """全部安全信息含实际FX、None和有序重复条款均进入hash。"""
    return canonical_creation_hash(
        {"version": "quote-approval-payload-v1", "payload": payload}
    )


def quote_approval_facts_hash(facts: tuple[QuoteApprovalFact, ...]) -> Hash:
    """仅独立决定快照参与；APPROVED→APPLIED不改变成功身份。"""
    if len({f.approval_type for f in facts}) != len(facts):
        raise QuoteApprovalError("approval_fact_invalid")
    snapshots = tuple(
        QuoteApprovalDecisionSnapshot(
            **{
                name: getattr(f, name)
                for name in QuoteApprovalDecisionSnapshot.model_fields
            }
        )
        for f in sorted(facts, key=lambda f: APPROVAL_TYPES.index(f.approval_type))
    )
    return canonical_creation_hash(
        {"version": "quote-approval-facts-v1", "decisions": snapshots}
    )


def require_quote_approval_access(
    subject: QuoteApprovalSubject,
    context: QuoteApprovalAccessContext,
    *,
    action: Literal["read", "decide", "apply"],
) -> None:
    """当前直属manager或boss可独立决定；本人起草/负责仅增加读权。"""
    actor, owner = context.actor, context.owner
    if (
        subject.tenant_id != context.tenant_id
        or actor.tenant_id != subject.tenant_id
        or owner.tenant_id != subject.tenant_id
        or not actor.is_active
        or subject.opportunity_id != context.opportunity_id
        or subject.prepared_by != context.prepared_by
        or subject.submitted_owner_id != context.submitted_owner_id
    ):
        raise QuoteApprovalPermissionError("permission_denied")
    responsible = {subject.prepared_by, subject.submitted_owner_id, owner.employee_id}
    jurisdiction = actor.role == "boss" or (
        actor.role == "manager"
        and owner.is_active
        and owner.manager_id == actor.employee_id
    )
    if action == "read":
        allowed = jurisdiction or actor.employee_id in responsible
    elif action in {"decide", "apply"}:
        allowed = jurisdiction and actor.employee_id not in responsible
    else:
        allowed = False
    if not allowed:
        raise QuoteApprovalPermissionError("permission_denied")
