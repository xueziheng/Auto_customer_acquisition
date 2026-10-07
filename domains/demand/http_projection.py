"""需求安全投影与原单位规则同域，不读取原件或改变持久状态。"""

from domains.demand.http_schemas import (
    NeedUnitConfirmationPublicView,
    NeedUnitPreparationView,
)
from domains.demand.schemas import NeedQuoteFacts, NeedUnitConfirmationView
from domains.demand.unit_facts import assess_quote_preparation
from shared.schemas.provenance import summarize_provenance


def project_need_unit_preparation(facts: NeedQuoteFacts) -> NeedUnitPreparationView:
    """保留真实历史值，只有状态说明它是否仍适用于当前数量。"""
    assessment = assess_quote_preparation(facts)
    return NeedUnitPreparationView(
        need_id=facts.need_id,
        account_id=facts.account_id,
        quantity=facts.quantity.value if facts.quantity is not None else None,
        quantity_fact_hash=assessment.quantity_fact_hash,
        unit=facts.unit.value if facts.unit is not None else None,
        unit_confirmation_id=facts.unit_confirmation_id,
        quantity_status=assessment.quantity_status,
        unit_status=assessment.unit_status,
        quantity_origin=summarize_provenance(facts.quantity.provenance)
        if facts.quantity is not None
        else None,
        unit_origin=summarize_provenance(facts.unit.provenance)
        if facts.unit is not None
        else None,
    )


def project_need_unit_confirmation(
    value: NeedUnitConfirmationView,
) -> NeedUnitConfirmationPublicView:
    """逐字段裁剪来源，仅保留receipt可发现身份与安全Provenance。"""
    return NeedUnitConfirmationPublicView(
        need_id=value.need_id,
        confirmation_id=value.confirmation_id,
        quantity_fact_hash=value.quantity_fact_hash,
        unit=value.unit.value,
        confirmed_by=value.confirmed_by,
        confirmed_at=value.confirmed_at,
        source_message_id=value.source.source_message_id,
        artifact_id=value.source.artifact_id,
        content_hash=value.source.content_hash,
        observed_at=value.source.observed_at,
        unit_origin=summarize_provenance(value.unit.provenance),
    )
