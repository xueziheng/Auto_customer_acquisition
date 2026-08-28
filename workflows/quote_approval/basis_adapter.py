"""workflow显式等值适配两个域公开DTO；不透传Any、不改写证据语义。"""
from domains.costing import schemas as cost
from domains.quotations import schemas as quote
from domains.quotations.errors import QuotationError
from shared.schemas.identifiers import TenantId
from shared.schemas.money import Money
from shared.schemas.quote_creation import QuoteCreationIntent, QuoteRoundingInput


def _source(value: cost.SourceEvidence) -> quote.QuoteEvidenceSource:
    """逐字段保留SourceEvidence所有值，不以同名dict透传。"""
    return quote.QuoteEvidenceSource(
        tenant_id=value.tenant_id,
        source_ref=value.source_ref,
        artifact_id=value.artifact_id,
        content_hash=value.content_hash,
        locator=value.locator,
        observed_at=value.observed_at,
        source_type=value.source_type,
        source_url=value.source_url,
    )


def _policy(value: cost.PricingPolicyView) -> quote.QuotePolicySnapshot:
    """逐字段保留PricingPolicyView所有值，不以同名dict透传。"""
    return quote.QuotePolicySnapshot(
        policy_id=value.policy_id,
        content_hash=value.content_hash,
        category=value.category,
        minimum_margin_rate=value.minimum_margin_rate,
        target_margin_rate=value.target_margin_rate,
        cost_groups=value.cost_groups,
        effective_from=value.effective_from,
        source_ref=value.source_ref,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        source=_source(value.source) if value.source is not None else None,
        field_provenance=value.field_provenance,
    )


def _item_binding(value: cost.CostItemBinding) -> quote.QuoteCostItemBinding:
    """逐字段保留CostItemBinding所有值，不以同名dict透传。"""
    return quote.QuoteCostItemBinding(
        item_sequence=value.item_sequence,
        evidence_id=value.evidence_id,
        source_line_ref=value.source_line_ref,
        allocation_scope=value.allocation_scope,
    )


def _decision(value: cost.CostCoverageDecision) -> quote.QuoteCoverageDecision:
    """逐字段保留CostCoverageDecision所有值，不以同名dict透传。"""
    return quote.QuoteCoverageDecision(
        item_type=value.item_type,
        applicable=value.applicable,
        reason=value.reason,
        item_bindings=tuple(_item_binding(item) for item in value.item_bindings),
    )


def _coverage(value: cost.CostCoverageView) -> quote.QuoteCoverageSnapshot:
    """逐字段保留CostCoverageView所有值，不以同名dict透传。"""
    return quote.QuoteCoverageSnapshot(
        expected_sheet_hash=value.expected_sheet_hash,
        decisions=tuple(_decision(item) for item in value.decisions),
        acquisition_mode=value.acquisition_mode,
        coverage_id=value.coverage_id,
        cost_sheet_id=value.cost_sheet_id,
        content_hash=value.content_hash,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        field_provenance=value.field_provenance,
    )


def _scope_binding(value: cost.CostScopeEvidenceBinding) -> quote.QuoteScopeEvidenceBinding:
    """逐字段保留CostScopeEvidenceBinding所有值，不以同名dict透传。"""
    return quote.QuoteScopeEvidenceBinding(
        evidence_id=value.evidence_id,
        evidence_hash=value.evidence_hash,
        applicability_note=value.applicability_note,
    )


def _scope(value: cost.CostScopeConfirmationView) -> quote.QuoteScopeConfirmation:
    """逐字段保留CostScopeConfirmationView所有值，不以同名dict透传。"""
    return quote.QuoteScopeConfirmation(
        tenant_id=value.tenant_id,
        confirmation_id=value.confirmation_id,
        opportunity_id=value.opportunity_id,
        need_id=value.need_id,
        cost_sheet_id=value.cost_sheet_id,
        sheet_hash=value.sheet_hash,
        coverage_id=value.coverage_id,
        coverage_hash=value.coverage_hash,
        need_facts=value.need_facts,
        need_facts_hash=value.need_facts_hash,
        specification=value.specification,
        specification_hash=value.specification_hash,
        terms=value.terms,
        terms_hash=value.terms_hash,
        valid_until=value.valid_until,
        evidence_bindings=tuple(_scope_binding(item) for item in value.evidence_bindings),
        content_hash=value.content_hash,
        provenance=value.provenance,
    )


def _metrics(value: cost.ProfitMetrics) -> quote.QuoteProfitMetrics:
    """逐字段保留ProfitMetrics所有值，不以同名dict透传。"""
    return quote.QuoteProfitMetrics(
        unit_full_cost=value.unit_full_cost,
        minimum_price=value.minimum_price,
        target_price=value.target_price,
        gross_profit=value.gross_profit,
        contribution_profit=value.contribution_profit,
        full_cost_profit=value.full_cost_profit,
        margin_rate=value.margin_rate,
        discount_headroom=value.discount_headroom,
        additional_acquisition_headroom=value.additional_acquisition_headroom,
    )


def _calculation(value: cost.CalculationSnapshot) -> quote.QuoteCalculationSnapshot:
    """逐字段保留CalculationSnapshot所有值，不以同名dict透传。"""
    return quote.QuoteCalculationSnapshot(
        cost_sheet_id=value.cost_sheet_id,
        policy_id=value.policy_id,
        inputs_hash=value.inputs_hash,
        context_hash=value.context_hash,
        version_number=value.version_number,
        computed_at=value.computed_at,
        base_currency=value.base_currency,
        quote_currency=value.quote_currency,
        metrics=_metrics(value.metrics),
        effective_unit_revenue=value.effective_unit_revenue,
        displayed_unit_price=value.displayed_unit_price,
        displayed_total=value.displayed_total,
    )


def _options(value: cost.PricingOptions) -> quote.QuotePricingOptions:
    """逐字段保留PricingOptions所有值，不以同名dict透传。"""
    return quote.QuotePricingOptions(
        mode=value.mode,
        unit_price=value.unit_price,
        rounding=QuoteRoundingInput(unit_places=value.rounding.unit_places, total_places=value.rounding.total_places, strategy=value.rounding.strategy),
        quote_fx=value.quote_fx,
        algorithm_version=value.algorithm_version,
    )


def _fx(value: cost.QuoteFxView) -> quote.QuoteFxSnapshot:
    """逐字段保留QuoteFxView所有值，不以同名dict透传。"""
    return quote.QuoteFxSnapshot(
        source=_source(value.source),
        field_provenance=value.field_provenance,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        fx_id=value.fx_id,
        content_hash=value.content_hash,
        base_currency=value.base_currency,
        quote_currency=value.quote_currency,
        source_ref=value.source_ref,
        rate=value.rate,
        observed_at=value.observed_at,
    )


def to_quote_basis(value: cost.FrozenCostBasis) -> quote.QuoteBasis:
    """逐字段保留FrozenCostBasis所有值，不以同名dict透传。"""
    return quote.QuoteBasis(
        tenant_id=value.tenant_id,
        basis_id=value.basis_id,
        operation_id=value.operation_id,
        request_hash=value.request_hash,
        opportunity_id=value.opportunity_id,
        cost_sheet_id=value.cost_sheet_id,
        context_hash=value.context_hash,
        sheet_hash=value.sheet_hash,
        basis_hash=value.basis_hash,
        policy_id=value.policy_id,
        quantity=value.quantity,
        specification=value.specification,
        unit=value.unit,
        destination=value.destination,
        need_facts=value.need_facts,
        scope_confirmation=_scope(value.scope_confirmation),
        policy=_policy(value.policy),
        coverage=_coverage(value.coverage),
        calculation=_calculation(value.calculation),
        price_evidence=tuple(_price(item, value.tenant_id) for item in value.price_evidence),
        pricing_options=_options(value.pricing_options),
        cost_fx_rates=value.cost_fx_rates,
        quote_fx=_fx(value.quote_fx) if value.quote_fx is not None else None,
        valid_until=value.valid_until,
        frozen_at=value.frozen_at,
    )


def _price(value: cost.PriceEvidenceView, tenant_id: TenantId) -> quote.QuotePriceEvidence:
    """两个真实证据分支各自完整构造，原币种与金额合为Money。"""
    if value.source.tenant_id != tenant_id:
        raise QuotationError("basis_mismatch")
    if isinstance(value, cost.SupplierPriceEvidenceView):
        return quote.QuoteSupplierEvidence(
            tenant_id=tenant_id,
            amount=Money(value.amount, value.currency),
            source=_source(value.source),
            kind=value.kind,
            opportunity_id=value.opportunity_id,
            evidence_id=value.evidence_id,
            evidence_hash=value.evidence_hash,
            source_ref=value.source_ref,
            locator=value.locator,
            field_provenance=value.field_provenance,
            confirmed_by=value.confirmed_by,
            confirmed_at=value.confirmed_at,
            need_id=value.need_id,
            supplier_ref=value.supplier_ref,
            specification=value.specification,
            unit=value.unit,
            destination=value.destination,
            basis=value.basis,
            quantity_min=value.quantity_min,
            quantity_max=value.quantity_max,
            moq=value.moq,
            quoted_at=value.quoted_at,
            valid_until=value.valid_until,
        )
    if isinstance(value, cost.ExpenseEvidenceView):
        return quote.QuoteExpenseEvidence(
            tenant_id=tenant_id,
            amount=Money(value.amount, value.currency),
            source=_source(value.source),
            kind=value.kind,
            opportunity_id=value.opportunity_id,
            evidence_id=value.evidence_id,
            evidence_hash=value.evidence_hash,
            source_ref=value.source_ref,
            locator=value.locator,
            field_provenance=value.field_provenance,
            confirmed_by=value.confirmed_by,
            confirmed_at=value.confirmed_at,
            item_type=value.item_type,
            allocation_scope=value.allocation_scope,
            is_per_unit=value.is_per_unit,
            quantity=value.quantity,
            basis=value.basis,
            observed_at=value.observed_at,
            valid_until=value.valid_until,
        )
    raise QuotationError("basis_mismatch")


def pricing_options_from_intent(intent: QuoteCreationIntent) -> cost.PricingOptions:
    """人工报价选项只引用intent，报价FX由冻结域按独立ref解析。"""
    return cost.PricingOptions(mode="manual",unit_price=intent.unit_price,
        rounding=cost.RoundingPolicy(unit_places=intent.rounding.unit_places,total_places=intent.rounding.total_places,
            strategy=intent.rounding.strategy),quote_fx=None,algorithm_version="costing-v1")
