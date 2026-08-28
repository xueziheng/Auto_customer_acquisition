"""各类成本事实逐值白名单投影，不以黑名单裁剪内部模型。"""

from domains.costing.http_schemas import (
    CostCoveragePublicView,
    CostScopePublicView,
    ExpenseEvidencePublicView,
    PriceEvidencePublicView,
    PricingPolicyPublicView,
    PricingSourceSummary,
    QuoteFxPublicView,
    SupplierPriceEvidencePublicView,
)
from domains.costing.schemas import (
    CostCoverageView,
    CostScopeConfirmationView,
    PriceEvidenceView,
    PricingPolicyView,
    QuoteFxView,
    SourceEvidence,
)
from shared.schemas.provenance import summarize_provenance


def project_source(value: SourceEvidence) -> PricingSourceSummary:
    """仅来源身份、hash、类型和观察时间，不带可展开地址。"""
    return PricingSourceSummary(
        source_ref=value.source_ref,
        artifact_id=value.artifact_id,
        content_hash=value.content_hash,
        source_type=value.source_type,
        observed_at=value.observed_at,
    )


def project_price_evidence(value: PriceEvidenceView) -> PriceEvidencePublicView:
    """判别分支分别保留其真实商业字段，不添加另一类不存在的字段。"""
    if value.kind == "supplier_price":
        return SupplierPriceEvidencePublicView(
            kind=value.kind,
            evidence_id=value.evidence_id,
            evidence_hash=value.evidence_hash,
            opportunity_id=value.opportunity_id,
            currency=value.currency,
            amount=value.amount,
            confirmed_by=value.confirmed_by,
            confirmed_at=value.confirmed_at,
            source=project_source(value.source),
            field_provenance={
                k: summarize_provenance(p) for k, p in value.field_provenance.items()
            },
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
    return ExpenseEvidencePublicView(
        kind=value.kind,
        evidence_id=value.evidence_id,
        evidence_hash=value.evidence_hash,
        opportunity_id=value.opportunity_id,
        currency=value.currency,
        amount=value.amount,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        source=project_source(value.source),
        field_provenance={
            k: summarize_provenance(p) for k, p in value.field_provenance.items()
        },
        item_type=value.item_type,
        allocation_scope=value.allocation_scope,
        is_per_unit=value.is_per_unit,
        quantity=value.quantity,
        basis=value.basis,
        observed_at=value.observed_at,
        valid_until=value.valid_until,
    )


def project_policy(value: PricingPolicyView) -> PricingPolicyPublicView:
    """历史source可空保持原值，不凭空补造确认来源。"""
    return PricingPolicyPublicView(
        policy_id=value.policy_id,
        content_hash=value.content_hash,
        category=value.category,
        minimum_margin_rate=value.minimum_margin_rate,
        target_margin_rate=value.target_margin_rate,
        cost_groups=value.cost_groups,
        effective_from=value.effective_from,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        source=project_source(value.source) if value.source is not None else None,
        field_provenance={
            k: summarize_provenance(p) for k, p in value.field_provenance.items()
        },
    )


def project_quote_fx(value: QuoteFxView) -> QuoteFxPublicView:
    """方向性汇率原值，无倒数、自动查询或隐式币种。"""
    return QuoteFxPublicView(
        fx_id=value.fx_id,
        content_hash=value.content_hash,
        base_currency=value.base_currency,
        quote_currency=value.quote_currency,
        rate=value.rate,
        observed_at=value.observed_at,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        source=project_source(value.source),
        field_provenance={
            k: summarize_provenance(p) for k, p in value.field_provenance.items()
        },
    )


def project_coverage(value: CostCoverageView) -> CostCoveragePublicView:
    """确认后的精确持久快照，不把hash回执当当前latest。"""
    return CostCoveragePublicView(
        coverage_id=value.coverage_id,
        cost_sheet_id=value.cost_sheet_id,
        content_hash=value.content_hash,
        expected_sheet_hash=value.expected_sheet_hash,
        decisions=value.decisions,
        acquisition_mode=value.acquisition_mode,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        field_provenance={
            k: summarize_provenance(p) for k, p in value.field_provenance.items()
        },
    )


def project_scope(value: CostScopeConfirmationView) -> CostScopePublicView:
    """不携完整Need事实，当前需求应重新读准备摘要。"""
    return CostScopePublicView(
        confirmation_id=value.confirmation_id,
        opportunity_id=value.opportunity_id,
        need_id=value.need_id,
        cost_sheet_id=value.cost_sheet_id,
        sheet_hash=value.sheet_hash,
        coverage_id=value.coverage_id,
        coverage_hash=value.coverage_hash,
        need_facts_hash=value.need_facts_hash,
        specification=value.specification,
        specification_hash=value.specification_hash,
        terms=value.terms,
        terms_hash=value.terms_hash,
        valid_until=value.valid_until,
        evidence_bindings=value.evidence_bindings,
        content_hash=value.content_hash,
        provenance=summarize_provenance(value.provenance),
    )
