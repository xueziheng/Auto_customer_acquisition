"""报价不可变内容、第二道门与确定性投影；无IO和批准捷径。"""

from datetime import datetime
from decimal import Context, localcontext

from domains.quotations.basis_schemas import QuoteBasis, QuoteEvidenceConfirmation, QuoteSupplierEvidence
from domains.quotations.context import QuoteBusinessContext, QuoteSpecificationFacts, quote_specification
from domains.quotations.context import QuoteIssuer
from domains.quotations.errors import QuotationError, QuotationUnavailableError
from domains.quotations.version_schemas import QuoteContentLine, QuoteContentSnapshot
from shared.schemas.identifiers import QuoteId
from shared.schemas.quote_creation import QuoteCreationIntent, canonical_creation_hash, quote_creation_request_hash, quote_terms_hash
from shared.schemas.quote_facts import fact_utc


def format_quote_specification(spec: QuoteSpecificationFacts) -> str:
    """固定英文标签顺序展示全部已确认规格，不改写值或填补None。"""
    fields = (("product_category", "Product category"), ("application", "Application"),
        ("material", "Material"), ("size_spec", "Size"), ("packaging", "Packaging"),
        ("certification_required", "Certification required"))
    return "\n".join(f"{label}: {getattr(spec, name)}" for name, label in fields if getattr(spec, name) is not None)


def _hash(values: dict[str, object]) -> str:
    """新内容编码包含完整嵌套来源，不改变T3A/B既有hash。"""
    try:
        return canonical_creation_hash({"version": "quote-content-v1", "content": values})
    except (ValueError, ArithmeticError, TypeError):
        raise QuotationError("invalid_input") from None


def quote_content_hash(content: QuoteContentSnapshot) -> str:
    """只排除自身hash与创建时钟，所有其他内容均参与绑定。"""
    return _hash({name: getattr(content, name) for name in type(content).model_fields
        if name not in {"content_hash", "created_at"}})


def build_quote_content(quote_id: QuoteId, version: int, intent: QuoteCreationIntent,
    basis: QuoteBasis, context: QuoteBusinessContext, *, created_at: datetime,
    replaced_quote_version: int | None) -> QuoteContentSnapshot:
    """先构建固定完整载荷再计算hash，不以伪hash构造临时快照。"""
    spec = quote_specification(context.need_facts)
    line = QuoteContentLine(line_number=1, description=spec.product_category,
        specification=format_quote_specification(spec), unit=context.unit, quantity=context.quantity,
        unit_price=basis.calculation.displayed_unit_price, line_total=basis.calculation.displayed_total,
        rounding=intent.rounding)
    values = dict(tenant_id=context.tenant_id, quote_id=quote_id, opportunity_id=context.opportunity_id,
        version=version, operation_id=basis.operation_id, request_hash=basis.request_hash, intent=intent,
        basis=basis, prepared_by=intent.prepared_by, owner_id=context.owner_id, issuer=context.issuer,
        account_name=context.account_name, country=context.country, lines=(line,), terms=intent.terms,
        valid_until=intent.valid_until, replaces_quote_id=intent.replaces_quote_id,
        replaced_quote_version=replaced_quote_version)
    return QuoteContentSnapshot(**values, created_at=created_at, content_hash=_hash(values))


def require_content_integrity(content: QuoteContentSnapshot) -> None:
    """每次读取/写入验证存储内容，不把frozen误解为嵌套dict不可变。"""
    if quote_content_hash(content) != content.content_hash:
        raise QuotationUnavailableError("storage_inconsistent")


def quote_issuer_hash(issuer: QuoteIssuer) -> str:
    """抬头hash完整绑定三字段与人工来源确认，不包含自身hash。"""
    return canonical_creation_hash({"version":"quote-issuer-v1", "issuer":
        {n:getattr(issuer,n) for n in type(issuer).model_fields if n!="content_hash"}})


def validate_terms(intent: QuoteCreationIntent) -> None:
    """类型不能暗渡另一类承诺；仍需T5对每个类型逐次审批。"""
    from domains.quotations.service import contains_forbidden_commitment
    permitted = {"discount": "discount", "delivery_commitment": "delivery_date_commitment",
        "payment_terms": "payment_terms", "certification_commitment": "certification_commitment"}
    for term in intent.terms:
        hits = contains_forbidden_commitment(term.text.replace("\n", " "))
        if any(hit.value != permitted[term.kind] for hit in hits):
            raise QuotationError("unsupported_term")


def _confirmation(value: QuoteEvidenceConfirmation, tenant_id: str, now: datetime) -> None:
    """同一来源/确认事实完整保留，不扩展任何原件权限。"""
    source = value.source
    if (source.tenant_id != tenant_id or source.observed_at > now or value.confirmed_at > now
        or not value.field_provenance
        or (source.source_type == "web_page" and not source.source_url)):
        raise QuotationError("evidence_invalid")
    for provenance in value.field_provenance.values():
        if (not provenance.is_human_confirmed or provenance.confirmed_by != value.confirmed_by
            or provenance.confirmed_at != value.confirmed_at or provenance.source_id != source.source_ref
            or provenance.source_type.value != source.source_type or provenance.source_url != source.source_url
            or (source.source_type == "web_page" and provenance.page_hash != source.content_hash)):
            raise QuotationError("evidence_invalid")


def validate_quote_basis(intent: QuoteCreationIntent, basis: QuoteBasis,
    context: QuoteBusinessContext, *, now: datetime) -> None:
    """第二道报价硬门；确认语义来自scope，不比较供应商自由文本，不重算利润。"""
    now = fact_utc(now)
    validate_terms(intent)
    try:
        request_hash = quote_creation_request_hash(intent)
    except (ValueError, ArithmeticError):
        raise QuotationError("invalid_input") from None
    if intent.expected_context_hash != context.context_hash or basis.context_hash != context.context_hash:
        raise QuotationError("context_changed")
    if (intent.tenant_id, intent.opportunity_id, intent.cost_sheet_id, intent.expected_sheet_hash,
        request_hash, context.quantity, context.specification, context.unit, context.destination) != (
        basis.tenant_id, basis.opportunity_id, basis.cost_sheet_id, basis.sheet_hash,
        basis.request_hash, basis.quantity, basis.specification, basis.unit, basis.destination
    ) or basis.tenant_id != context.tenant_id or basis.opportunity_id != context.opportunity_id:
        raise QuotationError("basis_mismatch")
    scope = basis.scope_confirmation
    if (scope.tenant_id, scope.opportunity_id, scope.need_id, scope.cost_sheet_id, scope.sheet_hash,
        scope.confirmation_id, scope.content_hash, scope.need_facts, scope.need_facts_hash,
        scope.specification, scope.specification_hash, scope.terms, scope.terms_hash, scope.valid_until) != (
        context.tenant_id, context.opportunity_id, context.need_id, intent.cost_sheet_id, intent.expected_sheet_hash,
        intent.scope_confirmation_id, intent.scope_confirmation_hash, context.need_facts, context.need_facts_hash,
        context.specification, context.specification_hash, intent.terms, quote_terms_hash(intent.terms), intent.valid_until
    ) or basis.need_facts != context.need_facts or basis.valid_until != intent.valid_until:
        raise QuotationError("scope_stale")
    if not scope.provenance.is_human_confirmed:
        raise QuotationError("scope_stale")
    if intent.valid_until <= now:
        raise QuotationError("quote_expired")
    coverage, calculation, options = basis.coverage, basis.calculation, basis.pricing_options
    if ((coverage.coverage_id, coverage.content_hash, coverage.cost_sheet_id, coverage.expected_sheet_hash)
        != (scope.coverage_id, scope.coverage_hash, basis.cost_sheet_id, basis.sheet_hash)
        or (calculation.cost_sheet_id, calculation.context_hash, calculation.policy_id)
        != (basis.cost_sheet_id, basis.context_hash, basis.policy_id)
        or basis.policy.policy_id != basis.policy_id or basis.policy.effective_from > now
        or basis.policy.confirmed_at > now or coverage.confirmed_at > now
        or basis.frozen_at > now or calculation.computed_at > now
        or options.mode != "manual" or options.unit_price != intent.unit_price or options.rounding != intent.rounding):
        raise QuotationError("basis_mismatch")
    fx = basis.quote_fx
    if (intent.quote_fx_ref is None) != (fx is None) or (fx and intent.quote_fx_ref != fx.fx_id):
        raise QuotationError("basis_mismatch")
    if fx:
        _confirmation(fx, context.tenant_id, now)
        if options.quote_fx is None or (options.quote_fx.base, options.quote_fx.quote, options.quote_fx.rate,
            options.quote_fx.source, options.quote_fx.observed_at) != (
            fx.base_currency, fx.quote_currency, fx.rate, fx.source_ref, fx.observed_at):
            raise QuotationError("basis_mismatch")
    elif options.quote_fx is not None or calculation.base_currency != calculation.quote_currency:
        raise QuotationError("basis_mismatch")
    with localcontext(Context(prec=80)):
        unit = intent.unit_price.round_to(intent.rounding.unit_places, intent.rounding.strategy)
        total = unit.multiply(basis.quantity).round_to(intent.rounding.total_places, intent.rounding.strategy)
    if (calculation.displayed_unit_price != unit or calculation.displayed_total != total
        or calculation.quote_currency != unit.currency or calculation.effective_unit_revenue.currency != calculation.base_currency):
        raise QuotationError("basis_mismatch")
    prices = {p.evidence_id:p for p in basis.price_evidence}
    if (len(prices) != len(basis.price_evidence) or not prices
        or len(scope.evidence_bindings) != len(prices)
        or {b.evidence_id:b.evidence_hash for b in scope.evidence_bindings} != {k:p.evidence_hash for k,p in prices.items()}):
        raise QuotationError("evidence_invalid")
    bound, sequences, source_lines = set(), set(), set()
    for decision in coverage.decisions:
        if decision.applicable != bool(decision.item_bindings):
            raise QuotationError("evidence_invalid")
        for binding in decision.item_bindings:
            price = prices.get(binding.evidence_id)
            if price is None or binding.item_sequence in sequences or binding.source_line_ref != price.locator:
                raise QuotationError("evidence_invalid")
            line = (price.source.artifact_id, binding.source_line_ref, binding.allocation_scope)
            if line in source_lines:
                raise QuotationError("evidence_invalid")
            source_lines.add(line)
            sequences.add(binding.item_sequence)
            bound.add(binding.evidence_id)
            if isinstance(price, QuoteSupplierEvidence):
                if decision.item_type != "product_purchase":
                    raise QuotationError("evidence_invalid")
            elif (decision.item_type != price.item_type or binding.allocation_scope != price.allocation_scope
                or price.quantity != basis.quantity or price.item_type == "product_purchase"):
                raise QuotationError("evidence_invalid")
    if bound != set(prices):
        raise QuotationError("evidence_invalid")
    suppliers = 0
    for price in prices.values():
        _confirmation(price, context.tenant_id, now)
        if (price.tenant_id != context.tenant_id or price.opportunity_id != context.opportunity_id
            or price.source_ref != price.source.source_ref or price.locator != price.source.locator or price.amount.amount < 0):
            raise QuotationError("evidence_invalid")
        if price.valid_until is not None and (price.valid_until <= now or price.valid_until < intent.valid_until):
            raise QuotationError("evidence_expired")
        if isinstance(price, QuoteSupplierEvidence):
            suppliers += 1
            if (price.basis != "quoted" or price.need_id != context.need_id or price.unit != context.unit
                or price.destination != context.destination or price.quoted_at > now
                or not price.quantity_min <= basis.quantity <= price.quantity_max or price.moq > basis.quantity):
                raise QuotationError("evidence_invalid")
        elif price.observed_at > now:
            raise QuotationError("evidence_invalid")
    if not suppliers:
        raise QuotationError("evidence_invalid")
