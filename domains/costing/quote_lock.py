"""报价冻结纯守卫：完整需求、人工映射及已有22项覆盖规则，不做IO。"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from domains.costing.errors import CostFreezeError, InvalidPricingEvidenceError
from domains.costing.freeze_schemas import (
    CostingContext,
    CostScopeConfirmationView,
    CostScopeEvidenceBinding,
    FrozenCostBasis,
)
from domains.costing.models import CostSheet, _require_numeric_precision
from domains.costing.schemas import (
    CostCoverageCreate,
    PriceEvidenceView,
    SupplierPriceEvidenceView,
)
from shared.errors import ValidationError
from shared.schemas.money import Money
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationOperationView,
    canonical_creation_hash,
    quote_creation_request_hash,
    quote_terms_hash,
    require_creation_decimal_resources,
)
from shared.schemas.quote_facts import canonical_fact_hash


def require_manual_price(price: Money) -> None:
    """人工原始单价沿用Numeric(28,12)边界，不限制T1派生输出的50位精度。"""
    try:
        require_creation_decimal_resources(price.amount)
        if price.amount <= 0:
            raise ValueError("人工单价必须为正")
        _require_numeric_precision(
            price.amount, precision=28, scale=12, field="人工单价"
        )
    except (ValueError, ValidationError):
        raise CostFreezeError("invalid_input") from None


def validate_cost_coverage(
    sheet: CostSheet,
    command: CostCoverageCreate,
    evidence: tuple[PriceEvidenceView, ...],
    *,
    now: datetime,
) -> None:
    """T2与冻结共用同一费用校验，禁止隐藏已确认项、重复分摊或混淆单件/整单。"""
    command = CostCoverageCreate.model_validate(
        command.model_dump(
            mode="python",
            include={"expected_sheet_hash", "decisions", "acquisition_mode"},
        )
    )
    confirmed = {
        item.item_sequence: item for item in sheet.items if item.entered_by is not None
    }
    if (
        None in confirmed
        or not confirmed
        or len(confirmed) != sum(item.entered_by is not None for item in sheet.items)
    ):
        raise InvalidPricingEvidenceError("成本表没有有效已确认明细或序号重复")
    prices = {item.evidence_id: item for item in evidence}
    bound: set[int] = set()
    lines: set[tuple[str, str, str]] = set()
    for decision in command.decisions:
        if decision.applicable and (
            (
                command.acquisition_mode == "summary"
                and decision.item_type
                in {"contact_data_cost", "ad_allocation", "agent_api_allocation"}
            )
            or (
                command.acquisition_mode == "detail"
                and decision.item_type == "customer_acquisition"
            )
        ):
            raise InvalidPricingEvidenceError("获客汇总与明细口径不能混用")
        for binding in decision.item_bindings:
            item = confirmed.get(binding.item_sequence)
            price = prices.get(binding.evidence_id)
            if (
                item is None
                or item.item_type.value != decision.item_type
                or binding.item_sequence in bound
                or price is None
            ):
                raise InvalidPricingEvidenceError("费用绑定未确认、类型错配或重复")
            if (
                price.opportunity_id != sheet.opportunity_id
                or item.source_ref
                not in {price.source_ref, price.source.artifact_id, price.evidence_id}
                or binding.source_line_ref != price.locator
                or item.amount.currency != price.currency
                or item.price_basis != price.basis
            ):
                raise InvalidPricingEvidenceError(
                    "费用来源、机会、币种或计价基准不一致"
                )
            line = (
                price.source.artifact_id,
                binding.source_line_ref,
                binding.allocation_scope,
            )
            if line in lines:
                raise InvalidPricingEvidenceError("同一原文费用明细及分摊范围重复")
            lines.add(line)
            if price.valid_until is not None and price.valid_until <= now:
                raise InvalidPricingEvidenceError("费用或采购依据已过期")
            if isinstance(price, SupplierPriceEvidenceView):
                if (
                    decision.item_type != "product_purchase"
                    or price.basis != "quoted"
                    or not price.quantity_min <= sheet.quantity <= price.quantity_max
                    or sheet.quantity < price.moq
                ):
                    raise InvalidPricingEvidenceError(
                        "采购必须为适用数量档内的供应商实报价"
                    )
                with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                    expected = (
                        price.amount
                        if item.is_per_unit
                        else price.amount * Decimal(sheet.quantity)
                    )
                if item.amount.amount != expected:
                    raise InvalidPricingEvidenceError(
                        "采购单件/整单金额与供应商单价不一致"
                    )
            elif (
                price.item_type != decision.item_type
                or price.amount != item.amount.amount
                or price.is_per_unit != item.is_per_unit
                or price.quantity != sheet.quantity
                or price.allocation_scope != binding.allocation_scope
            ):
                raise InvalidPricingEvidenceError("费用金额、适用数量或分摊口径不一致")
            bound.add(binding.item_sequence)
    if bound != set(confirmed):
        raise InvalidPricingEvidenceError("清单遗漏已确认费用或把已有费用标为不适用")


def require_context(context: CostingContext, sheet: CostSheet) -> None:
    """业务标量必须与完整Need一致，缺来源的事实不能参与新报价。"""
    f = context.need_facts
    if (context.tenant_id, context.opportunity_id) != (
        sheet.tenant_id,
        sheet.opportunity_id,
    ):
        raise CostFreezeError("context_changed")
    if (f.tenant_id, f.need_id, f.account_id) != (
        context.tenant_id,
        context.need_id,
        context.account_id,
    ):
        raise CostFreezeError("facts_corrupt")
    if context.need_facts_hash != canonical_fact_hash(
        {"version": "need-quote-facts-v1", "facts": f}
    ):
        raise CostFreezeError("facts_corrupt")
    if (
        f.quantity is None
        or f.quantity.value != context.quantity
        or sheet.quantity != context.quantity
    ):
        raise CostFreezeError("quantity_mismatch")
    if f.destination is None or f.destination.value != context.destination:
        raise CostFreezeError("destination_mismatch")
    if f.unit is None or f.unit.value != context.unit:
        raise CostFreezeError("unit_missing")
    if f.product_category.value != context.category:
        raise CostFreezeError("specification_mismatch")


def require_scope_evidence(
    context: CostingContext,
    evidence: tuple[PriceEvidenceView, ...],
    bindings: tuple[CostScopeEvidenceBinding, ...],
    *,
    valid_until: datetime,
    now: datetime,
) -> None:
    """人工映射不能替代quoted、单位/目的地、数量档与有效期硬门。"""
    expected = {item.evidence_id: item.evidence_hash for item in evidence}
    if (
        len(bindings) != len(expected)
        or {b.evidence_id: b.evidence_hash for b in bindings} != expected
    ):
        raise CostFreezeError("evidence_invalid")
    if tuple(b.evidence_id for b in bindings) != tuple(sorted(expected)):
        raise CostFreezeError("evidence_invalid")
    require_evidence_applicability(context, evidence, valid_until=valid_until, now=now)


def require_evidence_applicability(
    context: CostingContext,
    evidence: tuple[PriceEvidenceView, ...],
    *,
    valid_until: datetime | None,
    now: datetime,
) -> None:
    """测算和人工scope共用硬适用条件；测算不构造虚假人工映射。"""
    if valid_until is not None and valid_until <= now:
        raise CostFreezeError("evidence_expired")
    suppliers = 0
    for item in evidence:
        if (
            item.source.tenant_id != context.tenant_id
            or item.opportunity_id != context.opportunity_id
        ):
            raise CostFreezeError("evidence_invalid")
        if item.valid_until is not None and (
            item.valid_until <= now
            or (valid_until is not None and valid_until > item.valid_until)
        ):
            raise CostFreezeError("evidence_expired")
        if isinstance(item, SupplierPriceEvidenceView):
            suppliers += 1
            if item.need_id != context.need_id or item.basis != "quoted":
                raise CostFreezeError("evidence_invalid")
            if item.unit != context.unit:
                raise CostFreezeError("specification_mismatch")
            if item.destination != context.destination:
                raise CostFreezeError("destination_mismatch")
            if (
                not item.quantity_min <= context.quantity <= item.quantity_max
                or context.quantity < item.moq
            ):
                raise CostFreezeError("quantity_mismatch")
    if not suppliers:
        raise CostFreezeError("evidence_invalid")


def scope_content_hash(values: dict[str, object]) -> str:
    """确认身份和完整来源进入hash，唯一排除自身content_hash。"""
    return canonical_creation_hash(
        {
            "version": "cost-scope-confirmation-v1",
            **{name: value for name, value in values.items() if name != "content_hash"},
        }
    )


def cost_scope_hash(view: CostScopeConfirmationView) -> str:
    """公开纯hash，包含人工确认身份及所有映射和条款。"""
    return scope_content_hash(
        {name: getattr(view, name) for name in type(view).model_fields}
    )


def require_scope_integrity(view: CostScopeConfirmationView) -> None:
    """持久边界重验完整scope及确认Provenance，不仅信任一个hash。"""
    p = view.provenance
    if (
        view.content_hash != cost_scope_hash(view)
        or view.terms_hash != quote_terms_hash(view.terms)
        or view.need_facts_hash
        != canonical_fact_hash(
            {"version": "need-quote-facts-v1", "facts": view.need_facts}
        )
        or view.need_facts.tenant_id != view.tenant_id
        or view.need_facts.need_id != view.need_id
        or p.source_type.value != "employee_input"
        or p.source_id != view.confirmation_id
        or p.extracted_by != p.confirmed_by
        or p.extracted_at != p.confirmed_at
        or not p.is_human_confirmed
    ):
        raise CostFreezeError("facts_corrupt")


def require_completion(
    operation: QuoteCreationOperationView, receipt: QuoteCreationCompletion
) -> None:
    """回执须精确绑定原操作和修订链；不在此断言真实报价存在。"""
    intent = operation.intent
    if (
        receipt.tenant_id,
        receipt.operation_id,
        receipt.request_hash,
        receipt.basis_id,
        receipt.replaces_quote_id,
        receipt.replaced_quote_version,
        receipt.quote_version,
    ) != (
        operation.tenant_id,
        operation.operation_id,
        operation.request_hash,
        operation.basis_id,
        intent.replaces_quote_id,
        intent.expected_quote_version,
        (intent.expected_quote_version or 0) + 1,
    ):
        raise CostFreezeError("revision_conflict")
    if operation.completion is not None and operation.completion != receipt:
        raise CostFreezeError("revision_conflict")


def require_operation_integrity(operation: QuoteCreationOperationView) -> None:
    """持久读取不能信任错误的请求hash或不一致完成绑定。"""
    if operation.request_hash != quote_creation_request_hash(operation.intent):
        raise CostFreezeError("facts_corrupt")
    if operation.completion is not None:
        try:
            require_completion(operation, operation.completion)
        except CostFreezeError:
            raise CostFreezeError("facts_corrupt") from None


def basis_content_hash(values: dict[str, object]) -> str:
    """完整业务快照与操作身份进入hash，仅排除随机basisID/冻结时钟/自身。"""
    return canonical_creation_hash(
        {
            "version": "frozen-cost-basis-v1",
            **{
                name: value
                for name, value in values.items()
                if name not in {"basis_id", "basis_hash", "frozen_at"}
            },
        }
    )


def frozen_basis_hash(basis: FrozenCostBasis) -> str:
    """用于写入与严格恢复的相同快照身份。"""
    return basis_content_hash(
        {name: getattr(basis, name) for name in type(basis).model_fields}
    )


def require_basis_integrity(basis: FrozenCostBasis) -> None:
    """完整scope和所有嵌套快照绑定必须一致，不裁成不可复核的hash列表。"""
    from domains.costing.quote_service import view_content_hash

    require_scope_integrity(basis.scope_confirmation)
    s = basis.scope_confirmation
    evidence={item.evidence_id:item.evidence_hash for item in basis.price_evidence}
    if (len(evidence)!=len(basis.price_evidence) or evidence!={b.evidence_id:b.evidence_hash for b in s.evidence_bindings}
        or basis.policy.content_hash!=view_content_hash(basis.policy)
        or basis.coverage.content_hash!=view_content_hash(basis.coverage)
        or any(item.evidence_hash!=view_content_hash(item) for item in basis.price_evidence)
        or (basis.quote_fx is not None and basis.quote_fx.content_hash!=view_content_hash(basis.quote_fx))
        or basis.pricing_options.mode!="manual"):
        raise CostFreezeError("facts_corrupt")
    if (
        basis.basis_hash != frozen_basis_hash(basis)
        or (
            s.tenant_id,
            s.opportunity_id,
            s.cost_sheet_id,
            s.sheet_hash,
            s.need_facts,
            s.specification,
            s.valid_until,
        )
        != (
            basis.tenant_id,
            basis.opportunity_id,
            basis.cost_sheet_id,
            basis.sheet_hash,
            basis.need_facts,
            basis.specification,
            basis.valid_until,
        )
        or (basis.coverage.coverage_id, basis.coverage.content_hash)
        != (s.coverage_id, s.coverage_hash)
        or (
            basis.calculation.cost_sheet_id,
            basis.calculation.context_hash,
            basis.calculation.policy_id,
        )
        != (basis.cost_sheet_id, basis.context_hash, basis.policy_id)
        or basis.policy.policy_id != basis.policy_id
        or basis.need_facts.quantity is None
        or basis.quantity != basis.need_facts.quantity.value
        or basis.need_facts.unit is None
        or basis.unit != basis.need_facts.unit.value
        or basis.need_facts.destination is None
        or basis.destination != basis.need_facts.destination.value
    ):
        raise CostFreezeError("facts_corrupt")
