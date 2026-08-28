"""单位绑定与完整需求哈希：纯函数，禁止IO与数量类型修正。"""

from __future__ import annotations

from domains.demand.errors import NeedUnitError
from domains.demand.schemas import NeedQuoteFacts
from shared.schemas.identifiers import TenantId, ValidatedNeedId
from shared.schemas.provenance import FactualField
from shared.schemas.quote_facts import canonical_fact_hash, canonical_fact_value


def canonical_value(value: object) -> object:
    """稳定JSON表示含None的完整事实，不强转数量。"""
    try:
        return canonical_fact_value(value)
    except ValueError as exc:
        raise NeedUnitError("invalid_input") from exc


def canonical_hash(value: object) -> str:
    """UTF-8 canonical JSON SHA256，版本由调用者明确传入。"""
    return canonical_fact_hash(canonical_value(value))


def quantity_fact_hash(
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
    quantity: FactualField[int],
) -> str:
    """完整数量来源绑定，历史0仍可读。"""
    if type(quantity.value) is not int:
        raise NeedUnitError("quantity_invalid")
    return canonical_hash(
        {
            "version": "need-quantity-fact-v1",
            "tenant_id": tenant_id,
            "need_id": need_id,
            "quantity": quantity,
        }
    )


def need_quote_facts_hash(facts: NeedQuoteFacts) -> str:
    """全部事实与绑定纳入稳定hash，不包含读取时间。"""
    if facts.quantity is not None:
        quantity_fact_hash(facts.tenant_id, facts.need_id, facts.quantity)
    return canonical_hash({"version": "need-quote-facts-v1", "facts": facts})


def require_current_unit(facts: NeedQuoteFacts) -> FactualField[str]:
    """要求已确认正数量与仍匹配其完整来源的人工单位。"""
    quantity = facts.quantity
    if quantity is None or type(quantity.value) is not int or quantity.value <= 0:
        raise NeedUnitError("quantity_invalid")
    if not quantity.provenance.is_human_confirmed:
        raise NeedUnitError("fact_unconfirmed")
    if facts.unit is None or facts.unit_confirmation_id is None:
        raise NeedUnitError("unit_missing")
    if not facts.unit.provenance.is_human_confirmed:
        raise NeedUnitError("fact_unconfirmed")
    if facts.unit_quantity_fact_hash != quantity_fact_hash(
        facts.tenant_id, facts.need_id, quantity
    ):
        raise NeedUnitError("unit_stale")
    return facts.unit
