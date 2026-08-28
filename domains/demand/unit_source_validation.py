"""客户数量与逐字单位的最小必要词法条件，不做语义推断。"""

import re

from domains.demand.errors import NeedUnitError
from domains.demand.schemas import NeedUnitEvidenceQuery
from domains.demand.unit_facts import quantity_fact_hash
from shared.schemas.evidence_read import (
    NeedQuantitySourceFact,
    QuoteEvidenceError,
    parse_evidence_locator,
)
from shared.schemas.provenance import SourceType


def validate_need_unit_source_text(
    query: NeedUnitEvidenceQuery,
    current: NeedQuantitySourceFact,
    *,
    body: str,
    excerpt: str,
) -> None:
    """完整来源hash及相邻唯一整数/逐字单位，含糊语义仍由员工承担。"""
    try:
        quantity = current.quantity
        if (
            type(body) is not str
            or type(excerpt) is not str
            or (query.tenant_id, query.need_id, query.account_id)
            != (current.tenant_id, current.need_id, current.account_id)
            or quantity is None
            or type(quantity.value) is not int
            or quantity.value <= 0
            or quantity.provenance.source_type is not SourceType.CONVERSATION
            or not quantity.provenance.is_human_confirmed
            or quantity.provenance.source_id != query.source_message_id
            or not quantity.provenance.source_quote
            or quantity.provenance.source_quote not in body
            or excerpt != query.source_quote
            or excerpt not in body
            or quantity_fact_hash(current.tenant_id, current.need_id, quantity)
            != query.quantity_fact_hash
            or quantity_fact_hash(query.tenant_id, query.need_id, query.quantity)
            != query.quantity_fact_hash
        ):
            raise ValueError
        selection = parse_evidence_locator(query.locator)
        if (
            selection is None
            or selection.profile != "rfc822-plain-v1"
            or selection.page is not None
            or not 0 <= selection.start < selection.end <= len(body)
            or body[selection.start : selection.end] != excerpt
        ):
            raise ValueError
        digits = list(re.finditer(r"[0-9]+", excerpt))
        if (
            len(digits) != 1
            or digits[0].group() != str(quantity.value)
            or any(char.isdecimal() and not "0" <= char <= "9" for char in excerpt)
        ):
            raise ValueError
        token = digits[0]
        for index in (
            selection.start + token.start() - 1,
            selection.start + token.end(),
        ):
            if 0 <= index < len(body):
                char = body[index]
                if char.isalnum() or char in "_.,+-":
                    raise ValueError
        unit = re.escape(query.unit)
        matched = re.match(rf"\s+{unit}(?!\w)", excerpt[token.end() :])
        if matched is None:
            raise ValueError
        unit_end = selection.start + token.end() + matched.end()
        for index in (unit_end - len(query.unit) - 1, unit_end):
            if 0 <= index < len(body) and (body[index].isalnum() or body[index] == "_"):
                raise ValueError
        if len(list(re.finditer(rf"(?<!\w){unit}(?!\w)", excerpt))) != 1:
            raise ValueError
    except (ValueError, TypeError, AttributeError, NeedUnitError, QuoteEvidenceError):
        raise NeedUnitError("source_mismatch") from None
