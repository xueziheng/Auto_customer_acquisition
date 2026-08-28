"""已授权报价事实的显式安全白名单投影，不决定文件或原文权限。"""

from domains.quotations.context import QuoteIssuer
from domains.quotations.http_schemas import (
    QuoteInternalPublicView,
    QuoteIssuerPublicView,
    QuoteNeedPublicSummary,
)
from domains.quotations.version_schemas import QuoteDetailView
from shared.schemas.provenance import summarize_provenance
from shared.schemas.quote_facts import NeedQuoteFacts


def project_issuer(value: QuoteIssuer) -> QuoteIssuerPublicView:
    """保留逐字段确认，移除完整来源对象。"""
    return QuoteIssuerPublicView(
        issuer_id=value.issuer_id,
        content_hash=value.content_hash,
        name=value.name,
        address=value.address,
        contact=value.contact,
        source_ref=value.source_ref,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        field_provenance={
            key: summarize_provenance(p) for key, p in value.field_provenance.items()
        },
    )


def project_need(value: NeedQuoteFacts) -> QuoteNeedPublicSummary:
    """只投影确定的事实字段，来源缺失不会由推断填充。"""
    names = (
        "product_category",
        "application",
        "material",
        "size_spec",
        "packaging",
        "destination",
        "current_supply_issue",
        "certification_required",
        "unit",
        "quantity",
        "required_by",
        "target_price",
    )
    facts = {name: getattr(value, name) for name in names}
    return QuoteNeedPublicSummary(
        need_id=value.need_id,
        account_id=value.account_id,
        status=value.status,
        product_category=value.product_category.value,
        application=value.application.value if value.application is not None else None,
        material=value.material.value if value.material is not None else None,
        size_spec=value.size_spec.value if value.size_spec is not None else None,
        packaging=value.packaging.value if value.packaging is not None else None,
        destination=value.destination.value if value.destination is not None else None,
        current_supply_issue=value.current_supply_issue.value
        if value.current_supply_issue is not None
        else None,
        certification_required=value.certification_required.value
        if value.certification_required is not None
        else None,
        unit=value.unit.value if value.unit is not None else None,
        quantity=value.quantity.value if value.quantity is not None else None,
        required_by=value.required_by.value if value.required_by is not None else None,
        target_price=value.target_price.value
        if value.target_price is not None
        else None,
        unit_quantity_fact_hash=value.unit_quantity_fact_hash,
        unit_confirmation_id=value.unit_confirmation_id,
        origins={
            name: summarize_provenance(fact.provenance)
            for name, fact in facts.items()
            if fact is not None
        },
    )


def project_internal_quote(detail: QuoteDetailView) -> QuoteInternalPublicView:
    """版本字段均来自真实content，依据只暴露ID和既有计算结果。"""
    c = detail.content
    return QuoteInternalPublicView(
        quote_id=c.quote_id,
        opportunity_id=c.opportunity_id,
        version=c.version,
        state=detail.state,
        created_at=c.created_at,
        valid_until=c.valid_until,
        prepared_by=c.prepared_by,
        owner_id=c.owner_id,
        replaces_quote_id=c.replaces_quote_id,
        replaced_quote_version=c.replaced_quote_version,
        content_hash=c.content_hash,
        request_hash=c.request_hash,
        issuer=project_issuer(c.issuer),
        account_name=c.account_name,
        country=c.country,
        lines=c.lines,
        terms=c.terms,
        calculation=c.basis.calculation,
        basis_id=c.basis.basis_id,
        basis_hash=c.basis.basis_hash,
        cost_sheet_id=c.basis.cost_sheet_id,
        scope_confirmation_id=c.basis.scope_confirmation.confirmation_id,
    )
